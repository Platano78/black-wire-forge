# Motion craft knowledge — for the Motion Room Guide

Rule-per-line, grouped by room and then by the user's own MOMENT inside that room — idea → prompt/
line → settings → take → judging the result — since each engine needs its own prompt and the moments are where a user actually asks a question.
Labelled **observed** (read at the cited source) or **inferred** (my read connecting two observed
facts, or craft knowledge held with high confidence but not pulled from a single citable passage
today). Terms in `code font` are the app's own words. In-house sources were consulted before the web.

## The MOTION group's two rooms, and who serves them (before "idea" — orientation)

- **Video** room takes shots from words or a starting picture, or continues an earlier shot. Two
  engines serve it: **LTX-2.5** (modes `ltx`, `ltx_loop`) and **MiniMax-H3** (modes `fl2va`, `ref2v`,
  `continue`) — all five modes map to the `video` room. — observed, `rooms.json` id `video`,
  `engines/ltx.py` `mode_rooms`, `engines/minimax_h3.py` `mode_rooms`.
- **Talking Head** room takes one face picture and one line and renders a face that says it. Only
  **one** mode serves it: LTX's `talking` mode. MiniMax-H3 has no mode mapped to this room at all —
  its three modes all map to `video`. — observed (the mapping) and observed (as an absence),
  `engines/ltx.py` `mode_rooms` (`{"talking": "talking"}`) vs `engines/minimax_h3.py` `mode_rooms`
  (no `"talking"` key anywhere in it).
- Neither engine is named in the room; the room only shows what `/api/engines?lane=...` reports is
  `"available"` for that lane. A lane missing MiniMax-H3's model files still opens Video with LTX
  alone, and vice versa. — observed, AGENTS.md "Verify" section, `/api/engines` contract.
- 🔴 **This guide owns ONE shot at a time.** A script, a beat sheet, ordering several shots, or the
  cut/export belongs to the Cutting Room and its own Film Room Guide — never attempt multi-shot
  planning here; say so and hand off by name. — this is a consistency rule with the Film Room Guide,
  not a claim about the app's code.

---

## VIDEO room — LTX-2.5 (`ltx`, `ltx_loop`)

### Idea → prompt

- **The vendor prompt contract, from LTX's own published guide** (not this app's invention): one
  flowing paragraph naming the shot, the scene, the action, the character, the camera movement, and
  the audio, in present tense, with any spoken line in quotation marks — no token weights, no
  quality-tag tail. Write it the way a cinematographer would describe a shot list, not the way a
  poet describes a feeling: posture, gesture and visible expression instead of emotional labels
  ("sad", "confused"). Keep the scene focused — a few clear characters and actions read better than
  a crowded frame — and keep one coherent light source per shot; mixed lighting confuses the
  result. — observed, https://ltx.io/blog/ltx-2-5-prompt-guide (Lightricks' own LTX Blog; checked
  ).
- **This app's own field hint restates the two rules that matter most for its fixed 16:9 canvas**:
  16:9 only — "a square image produces distorted, weird motion" — and 1–2 actions max, both
  attributed to the vendor prompt contract directly in the field's own hint text. — observed,
  `engines/ltx.py` `fields["ltx"]` prompt hint, `mode_notes`/`prompt_guides["ltx"]`.
  🔴 **This is the one hard constraint the Guide must never let slip**: BWF's `ltx`/`ltx_loop`/
  `talking` stage-1 canvas must be a multiple of 32 in both dimensions — a square or portrait
  picture into `start_image`/`face` still gets sampled against that fixed latent and comes back
  distorted. "16:9 only" is the field's own advisory hint text, not something the code enforces —
  `_ltx_graph`'s width/height check (`engines/ltx.py:122-123`) validates ONLY that both dimensions
  are multiples of 32, not the aspect ratio itself. 1024×576 is a true 16:9 example (1.778); note
  that the app's own shipped defaults for `ltx_loop` and `talking` (both 768×512) are actually 3:2
  (1.5), not 16:9 — an inconsistency that originates in the app's own defaults, not something the
  Guide should paper over by calling 768×512 a 16:9 example. — observed, `engines/ltx.py`
  `fields["ltx"]`/`fields["ltx_loop"]`/`fields["talking"]` prompt/width/height hints (the "16:9
  only" advisory text) and `_ltx_graph` width/height validation (`engines/ltx.py:122-123`, the
  multiple-of-32 check only).
- 🔴 **The verbs decide who is in the shot, not the reference or the name.** A character referenced
  by a starting picture but given nothing to do can still be misread as costume on someone else; a
  person named in the prompt but not actually placed in the shot can get invented as a stranger.
  Give every character you want an active verb and a place in frame; name no one you don't want
  rendered. — inferred, generalised from a different (non-BWF) pipeline; this is
  craft caution here, not a BWF measurement.

### Settings

- `two_stage` (default on) sharpens with a second upscale-and-refine pass; turning it off halves
  VRAM and detail. The pack itself **refuses** the combination of an ending picture (morph) with
  `two_stage` on — the field's `enabled_when`/`disabled_reason` say so, and the graph raises a plain
  error if bypassed server-side. — observed, `engines/ltx.py` fields `two_stage`/`end_image`,
  `_ltx_graph` lines 137-143.
- `audio` (default on) generates picture and sound jointly, not as a separate pass — turning it off
  is the *only* way past the 993-frame length ceiling, since the empty-audio-latent generator
  (`LTXVEmptyLatentAudio`) itself caps there, not the audio decoder. — observed, `_ltx_graph`
  docstring + `length` field hint + `engines/ltx.py:90-91,146`.
- A **long single continuous take** (one held shot, not a cut) is possible via `context_length`
  (window size): sampling runs in overlapping temporal windows instead of one long pass, which is
  the only way a long clip finishes at all rather than grinding forever. Measured ceiling: about
  **41 seconds** held identity this way (the `long-single-take` preset: `length: 993,
  context_length: 121`). — observed, `engines/ltx.py` `context_length` field hint + `presets["ltx"]`
  `long-single-take` note
- **Chaining separate clips loses a character by about 7 seconds in** — a different mechanism
  (H3's `continue` mode, below) exists precisely because chaining loses identity; LTX's own answer
  to "longer than one take" is the windowed single take above, not chaining. — inferred, generalised
  from a different (non-BWF) LTX pipeline, so treat this as craft
  caution for BWF's own long-take preset, not a BWF-specific number.
- `ltx_loop` is video-only (no sound at all — the node predates LTX-2.5's joint audio latents) and
  has no length ceiling tied to audio, so it is the mode for "as long as possible, silent." —
  observed, `engines/ltx.py` `_ltx_loop_graph` docstring + `fields["ltx_loop"]` length range (up to
  16,289 frames).

### Take → judge

- **Camera movement between two still frames is not reliably judgeable — by the Guide's own text
  rules, or by the small model that runs behind it.** Real-render vision trial (below): asked
  whether a "slow push toward the lamp" LTX render's first/last frame showed a push, the framing in
  the two frames was in fact nearly identical (my own ground-truth read before asking), yet the
  model twice confidently answered "yes, significantly larger and closer" — a leading-question
  confirmation failure, not a rule-following failure; only an ABSOLUTE "never give a yes/no verdict"
  rule stopped it, softer "unless it's obviously large" wording did not. — observed, this
  guide's own vision trial, against a real BWF LTX render
  (job `9a666a763a02`, prompt "Slow push toward the lighthouse lamp...").
- A genuinely visible **position or pose change** between two attached stills (a hand moved from
  hovering to gripping a mechanism) IS something the Guide can describe plainly — that's direct
  comparison of two pictures, not a claim about motion smoothness or camera movement. — observed,
  same vision trial, job `e22264cb9dcb` ("an old keeper's hand winds a brass clockwork mechanism"),
  where the model correctly described the hand's position change once told this was allowed.

---

## VIDEO room — MiniMax-H3 (`fl2va`, `ref2v`, `continue`)

### Idea → prompt

- **`fl2va`** — a video from a starting picture and/or text; positive prompt only, both frames
  optional (omit both for pure text-to-video). — observed, `engines/minimax_h3.py`
  `prompt_guides["fl2va"]`.
- **`ref2v`** — up to 9 reference pictures and 3 reference clips; **describe the scene, and let H3
  speak any dialogue directly inside the prompt text — audio is never fed in as a separate
  text-to-speech step.** — observed, `engines/minimax_h3.py` `prompt_guides["ref2v"]` and
  `h3_ref2va_graph` docstring ("Audio is NEVER fed in as TTS").
- A `character` reference shot from more than one angle (front/side/back/detail), rather than one
  frontal image alone, gives a model a more accurate hold on the face — this exact recommendation
  appears in Kling's own Subject Binding guidance for a *different* video tool; not tested on BWF's H3 `ref2v` specifically, but the
  App's own field allows up to 9 reference pictures, so multiple angles fit within it. — inferred
  (a claim about a different tool, offered as craft caution), Kling's own public blog post on
  Subject Binding / Elements 3.0,
  cross-checked against `engines/minimax_h3.py` `fields["ref2v"]["ref_images"]` (`max: 9`).
- **`continue`** — 🔴 **the SAME shot carrying on, never a new angle or composition.** This is a
  binding rule, not a style preference: a new-composition prompt overrides the roughly 0.9 s of
  carried motion the previous shot hands it, measured as a hard cut (frame-difference ratio 12.1)
  against a true continuation (ratio 1.28). Name the same subject, the same camera move and the
  same framing as the shot before it. `prev_video` is a **cable**, not a file you pick — it only
  ever arrives already patched in from the shot before it on the timeline. — observed,
  `engines/minimax_h3.py` `prompt_guides["continue"]` (carries a full worked example in H3's
  structured format) and the app's own rule ("a continue shot's
  prompt must describe the SAME shot carrying on ... hard cut, ratio 12.1; as a continuation
  1.28"); `fields["continue"]["prev_video"]` hint for the cable/jack behaviour. **Confirmed against
  a real chain of four render jobs** in this guide's own vision trial: every `continue` shot in
  the chain (`25b4bbaee4cb` → `9ad82175d3b4` → `da761d214981` → `686ef20917e6`) used a prompt that
  described the same car on the same coastal road, and the chain reads as one continuing scene
  across all four renders, not a hard cut.
- **H3's structured prompt contract** (from MiniMax's own vendor spec, a level deeper than this
  app's plain field hints): open with an alignment line naming which picture/video/audio is
  referenced, then three labelled fields — `integrated_multimodal_description` (style, subject,
  what stays the same, the camera move as one of H3's named terms), `overall_soundscape`, and
  `non_diegetic_music`. `<Picture N>` / `<Video k>` tags must actually appear in the prompt text or
  the reference binds weakly. This is **not required** by BWF's own field hint (which just says
  "describe the scene"), but a hand-written version of it is worth reaching for when the plain form
  isn't landing. — observed, `engines/minimax_h3.py`.
- **Camera is a controlled vocabulary for H3**, not free English: `Zoom In/Out`, `Push In/Pull Out`,
  `Pan Left/Right`, `Truck Left/Right`, `Tilt Up/Down`, `Pedestal Up/Down`, `Arc Shot`,
  `Tracking Shot`, `Static Shot`, `Shake Slightly/Strongly`, `POV`, `Roll Clockwise/
  Counterclockwise`, each optionally modified by amplitude ("small"/"large") and speed
  ("slow"/"fast"). Write it as natural English inside the shot, e.g. "The camera pushes in with
  small amplitude at slow speed toward the table." — observed in this app.

### Settings

- **Fast mode (turbo LoRA)**: the lever that matters is the LoRA itself plus 8 steps, not step count
  alone — measured +2.30 dB over the 4-step LoRA and it removes a slow push-in the 4-step version
  introduces (5/5 seeds, 2026-09-21). BWF's own `fast`/`fast-plus`/`fast-best` quality tiers on
  `fl2va` and `continue` set `turbo_lora: true` at 4/6/8 steps respectively; "Fast best" (8 steps) is
  the one with this measured win, not merely the highest number in that group. — observed,
  `engines/minimax_h3.py` `presets["fl2va"]` `turbo-8step` note + `quality["fl2va"]`.
  A fresh 2026-09-23 re-test of 6-vs-8 steps on the *same* 4-step LoRA family did not reproduce
  the 8-step win and flagged its own baseline as suspect — **do not** cite that re-test as
  overturning the 8-step finding; it is unresolved, not a reversal. — observed in this app.
- `ref2v` has **no fast tier** — no speed-pack LoRA is wired for this path (a "Fast" tier here would run 4–8 steps with no distillation LoRA at all, exactly the mush
  the old page's speed-pack toggle existed to prevent). It starts at Middle (12 steps). —
  observed, `engines/minimax_h3.py` quality["ref2v"] comment.
- `ref_image_size: "max"` on `ref2v` reads referenced faces far better than the default ("match"
  rendered a character near-profile and generic) but measured **1.8× slower** (161.7 s vs 90.2 s). —
  observed, `engines/minimax_h3.py` `presets["ref2v"]` `max-reference` note.
- The delivered `continue` clip comes out about **22 frames (≈0.9 s) shorter** than the length you
  set — the join keeps some of the previous shot's tail frames to carry continuity, then trims them
  back off. Ask for slightly more than the beat needs. — observed, `engines/minimax_h3.py`
  `fields["continue"]["length"]` hint.

### Take → judge

- 🔴 **Licence gate — never suggest MiniMax-H3 output be shared publicly by a user in the excluded
  territories.** The MiniMax-H3 Community Licence's Applicable Territory is the world **excluding
  the EU, UK, Republic of Korea, and USA** — users there don't get the same automatic rights and
  must apply separately. — observed, `engines/minimax_h3.py` `licence` block, cross-checked against
  https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE and
  https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/QA-about-License.md. This is about the
  model's own licence, not something the Guide enforces — but it should be able to say so plainly
  if asked, per the app's own README rule ("call out ... territory-restricted [licences] by name").

---

## TALKING HEAD room — LTX `talking` (the only mode that serves this room)

### Idea → line

- The graph is the same i2v/joint-audio LTX build as the `ltx` mode, with two differences: the
  starting picture is called `face` instead of `start_image`, and the prompt is never typed by the
  user — it is composed by the app's own `talking_head_prompt()` from the `line`/`look` fields. —
  observed, `engines/ltx.py` `talking_graph`.
- **What `talking_head_prompt()` actually sends** (this is the real, only prompt this mode ever
  submits — the Guide should be able to say so plainly, not guess at a different shape):
  - With a line: `The person in the image looks directly into the camera and speaks. They say
    clearly: "<line>" Their mouth moves in sync with the words, natural lip movement, subtle head
    motion, natural blinking. Clean studio voice, close microphone, quiet room tone, no music.` plus
    an optional ` Shot: <look>.` tail.
  - With **no** line: a quiet listening shot instead of speech — `The person in the image looks
    directly into the camera, listening quietly. Subtle head motion, natural blinking. Quiet room
    tone, no speech, no music.` plus the same optional `Shot:` tail.
  — observed, `engines/ltx.py` `talking_head_prompt()`.
- 🔴 **The `look` field is a shot note, not a sentence subject** — it rides at the *end* of the
  composed prompt as `Shot: <look>.`, deliberately, because putting an instruction like "make her
  say it warmly" there as the grammatical subject once produced a garbled sentence ("make her say
  with warm lighting looks directly into the camera"). Write `look` as a short descriptive note
  (lighting, framing), never as an instruction aimed at the line. — observed, `engines/ltx.py`
  `talking_head_prompt()` docstring.

### Sizing the line to the clip's length

- **Conversational English runs close to 150 words per minute (≈2.5 words/second)**, with the page's
  own conversational band at 120–150 WPM (presentations 100–150, audiobooks/radio 150–160). —
  observed, https://virtualspeech.com/blog/average-speaking-rate-words-per-minute (re-checked
  directly against the live page's own figures, not just recalled).
  A faster figure sometimes cited for real telephone-conversation speech with pauses stripped out
  (~196–236 WPM, e.g. the Switchboard-corpus research behind Yuan/Liberman/Cieri 2006) is NOT on
  the page above and is not independently verified here — treat it as inferred/unconfirmed for this
  citation, not a fact pulled from the cited source.
- For sizing a Talking Head line, the safer number is the **low end of casual conversation, about
  2.2 words/second (~130 WPM)** rather than the 150 WPM midpoint, plus a ~15% cushion — a line that
  just fits at the midpoint rate risks running past a short clip once a natural pause or emphasis is
  added, and this mode has no measured "line lands late" cushion the way H3's dialogue path does
  (below). This is the Guide's own sizing choice, not a number the app or any source states
  directly — inferred, built from the observed WPM range above plus the observed absence of a
  landing-time measurement for this specific mode. **Verified as followable**: the live trial's own
  arithmetic (13 words → 5.9s → +15% → ~163 frames, rounded up) reproduced cleanly across 6 of 7
  separate runs on a 12B local model once stated as an explicit step-by-step procedure (one
  run showed a stochastic frame-conversion slip, not a formula error).
  🔴 **The live trial's own recommended frame count at the time (168) was itself invalid** — LTX
  requires `length % 8 == 1` (`engines/ltx.py:124`), and 168 fails that check outright, so the app's
  backend would have refused it. Caught by cross-model review, not by the live trial itself; fixed
  by adding an explicit "round up to the nearest valid frame count" step to the formula (169 frames,
  ~7.04s, is the corrected recommendation for that same 13-word line). **For now**: keep 2.2
  words/second, labelled inferred, until a real Talking Head render exists to test the actual
  landing-time against — this figure stays a placeholder for a measurement, not a final answer.
- **Two different calculations, not one formula run both ways.** Recommending a length FOR a line
  (above) includes the 15% cushion; finding how many words comfortably FIT a clip length someone
  already picked is the reverse and does not simply invert step 3 — divide the clip's seconds by
  (1.15 ÷ 2.2) ≈ 0.523 to get a safe word count. A 97-frame default clip (~4.0 s) comfortably fits
  roughly **7 words**; the 361-frame clip (~15.0 s, the UI's own upper range) fits roughly **28
  words**. Always compute against the clip's actual seconds (`length ÷ fps`, fps default 24), never
  a fixed word count.
- Length field: 9–993 frames, default 97 (~4 s at 24 fps), UI range 49–361 (~2–15 s). The field's
  own hint: "Longer than the line takes to say plays out as quiet listening afterwards" — so a short
  line in a long clip is not wasted, it just becomes a listening beat at the end. — observed,
  `engines/ltx.py` `fields["talking"]["length"]`.
- Measured render cost on this hardware, one setting only: 768×512 at 97 frames (~4 s), about a
  minute once the model is warm, longer on a cold first load. No other length/size combination is
  measured. — observed, `engines/ltx.py` `presets["talking"]` `measured-cost` note.

### Take → judge

- **No BWF-specific measurement exists for where in the clip an LTX talking-head line actually
  lands** (unlike MiniMax-H3's measured ~87%-into-the-clip finding for its own dialogue path, which
  is a *different* engine and mode, not this one) — flag this as genuinely unknown rather than
  reusing the H3 number. — observed (as an absence), no such note anywhere in `engines/ltx.py`.
- **Lip-sync accuracy is not judgeable from a still frame at all** — a still shows a mouth shape at
  one instant, never whether it tracked the audio over the clip's length. This is an absolute
  deferral, the same class of claim as camera movement between stills (see the Video room's LTX
  section above). — inferred, generalising the same still-vs-motion boundary this guide's
  vision trial found for camera movement; not separately tested with a real Talking Head render
  (none existed in the job history checked — see "Research log").

---

## What the room cannot judge — say so, don't guess

- **Likeness at full resolution is an absolute deferral.** Costume, hair silhouette and general
  shape are not likeness; only a full-resolution crop of the face settles it, and even then the
  Guide is describing a picture it was actually sent, never asserting an identity match on its own
  authority. **Confirmed in this guide's own vision trial**: shown two attached frames of the
  same driver from a real render chain and asked "is this the same person", the model correctly
  refused a verdict every time this rule was in force — the deferral held even under a leading
  question, unlike the camera-movement case below. — inferred (the general rule), generalised from the likeness law; observed (the trial result), vision probe
  V4.
- **Camera motion between two stills is not judgeable, full stop — this is now an ABSOLUTE rule,
  not a "when it's ambiguous" one.** A softer version of this rule ("only when the change is
  large and unambiguous") was tried first and failed on a real render: the model confidently
  confirmed a "significant" push-in between two frames that, on this session's own ground-truth
  look, were nearly identical in framing. Only removing the model's discretion entirely fixed it.
  — observed, this guide's own vision trial.
- **Lip-sync accuracy from a still** — never judgeable; a still shows one instant, not whether the
  mouth tracked the audio. — inferred, generalising the camera-motion finding above (not separately
  vision-tested; no Talking Head render existed to test against).
- Whether a rendered clip's audio track actually contains speech, rather than just having an audio
  stream at all. — inferred, generalised from the same craft caution the Film Room Guide's
  knowledge base states for the Cutting Room's own audio ("having an audio stream is not the same as
  containing sound") — no BWF Motion-specific measurement of this exists, so this is caution
  carried over from a sibling room, not a Motion-room finding.
- Whether a `continue` shot's motion genuinely reads as unbroken once cut into a sequence — that is
  a Cutting Room / Film Room Guide judgment (it can compare full-resolution frames across a cut),
  not something the Motion Room Guide can see from inside a single render.

## Research log

**In-house sources consulted first**: the app's own engine
packs (`engines/ltx.py`, `engines/minimax_h3.py`, `rooms.json`), and a survey of paid AI-film tools'
published UX (LTX Studio, Runway, Flora, Higgsfield, Kling and Google Flow). That survey turned out to be
scoped almost entirely to multi-shot/Cutting-Room concerns (script-to-shots, reference-role naming,
the cut/timeline) rather than single-shot craft — it already informed the Film Room Guide's own
knowledge base, and the one item that transfers to this room (Kling's angle-diversity tip for
character references) is cited above under H3 `ref2v`.

**Web sources, used only where in-house material was thin**: Lightricks' own LTX-2.5 prompting
guide (ltx.io/blog), MiniMax-H3's licence territory gate (Hugging Face model card, LICENSE file,
QA-about-License.md), and general English speaking-rate figures (VirtualSpeech, Word Counter Blog —
both aggregating published speech-tempo research, not primary academic sources themselves).

**Grounded against real renders, not just docs**: four `continue`-mode jobs from the app's own job
history (a genuine four-shot chain) and two `ltx`-mode jobs, downloaded via `/api/jobs` + `/api/view`
and split into stills with `ffmpeg`, with ground truth established by looking at the stills myself
before running any model probe.

Could not confirm: any BWF- or LTX-specific measurement of *where* a talking-head line lands inside
its clip; any multi-character composition finding run specifically on BWF's own `ltx`/`ltx_loop`
graphs (the "verbs decide who exists" and "chaining loses a character" cautions above are carried
over from a different (non-BWF) pipeline, and are
marked inferred for that reason); lip-sync accuracy or Talking Head timing against a real render —
no finished Talking Head job existed in this app instance's history to test against.
