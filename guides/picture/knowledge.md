# Picture-group craft knowledge — for the Picture Guide

Covers Black Wire Forge's (BWF) PICTURE group: **Picture** (`picture`, modes `t2i`/`edit`), **Pixel
Art** (`pixelart`), **Clean-up** (`cleanup`, modes `cutout`/`upscale`), and **Textures** (`textures`
— no engine installed yet). Rule-per-line, grouped by the moment where it applies inside a room —
**idea** (which room/mode) → **prompt** (the words) → **settings** (the fields/presets that change
the render) → **take** (what comes back, and its licence) → **judge/fix** (reading a finished
render honestly, "Not right? Tell the guide"). Each line ends with **observed** (read at the cited
source, or measured directly) or **inferred** (craft generalisation, or a claim about a different
model, held with reasonable confidence but not pulled from a citable passage today). Terms in
`code font` are the app's own field/preset ids — the guide must speak in these, not invented names.
Sourcing order: this app's own code and the maintainer's in-house scout notes first, then the model's own
card, then general web craft — see "Research log" for what came from where.

## IDEA — which room, which mode

- The engine behind Picture and Pixel Art is **Qwen-Image 2.1** (research licence, non-commercial) —
  text-to-image and edit in one model, up to 10 reference images for edit, 7 preset aspect ratios:
  1:1 (2048×2048), 4:3 (2400×1792), 3:4 (1792×2400), 3:2 (2528×1696), 2:3 (1696×2528), 16:9
  (2752×1536), 9:16 (1536×2752) — no preset is 2048×2752; that figure would wrongly conflate 1:1's
  2048 with 9:16's 2752. — observed, https://huggingface.co/Qwen/Qwen-Image-2.1. **Clean-up does
  NOT use Qwen-Image 2.1 at all** — `cutout` is BiRefNet only, `upscale` is RealESRGAN only; the
  pack only *shares the "image" capability category* with Picture's engine, nothing more. —
  observed, `engines/cleanup.py` module docstring + `roles`.
- **Picture** has two modes: `t2i` (a picture from words alone) and `edit` (change a picture you
  upload, at least one required). — observed, `engines/qwen_image.py` `mode_words`.
- **Pixel Art** is not a separate generator — under the hood it's Picture's own `t2i`/`edit` graph
  plus two fixed steps that always run after: background removal (BiRefNet), then a deterministic
  palette-lock/dither/downscale that can never drift, because it's a fixed transform, not a model
  call. — observed, `engines/pixelart.py` module docstring.
- **Clean-up** is two tools applied to an existing picture, not a generator: `cutout` (background
  removal, BiRefNet) and `upscale` (4x bigger, RealESRGAN x4plus) — neither takes a prompt. —
  observed, `engines/cleanup.py` `fields.cutout`, `fields.upscale`.
- **Textures** has **no engine installed** — the room exists in the app's navigation (`rooms.json`,
  group PICTURE, id `textures`, blurb "seamless tiles and surfaces for games") but its own
  empty-state text says so plainly: "Nothing installed makes this yet." Say the same thing honestly;
  don't paper over the gap. — observed, `rooms.json` (`textures` entry) + grep of `engines/` for a
  `textures` cap (none found).
- The honest redirect for a seamless-tile request today is Picture's `t2i` mode with its
  `seamless-tile` preset — a real, working control in a *different* room, not a substitute for a
  dedicated Textures engine and not the same thing as one. — observed, cross-reference to
  `engines/qwen_image.py` `presets.t2i["seamless-tile"]`.
- A bulk background-removal or "make it bigger" request belongs in Clean-up, not Picture's edit
  mode — Picture's own `remove-background` preset works but is ~10x slower than Clean-up's dedicated
  `cutout` (50s vs 4.7s, measured). — observed, `engines/qwen_image.py` `presets.edit`, cross-
  referenced against `engines/cleanup.py` `mode_notes.cutout`.

## PROMPT — writing the words

- **Positive prompt only, always.** Put anything to avoid in the separate "Things to avoid" field
  (`negative`), never folded into the main prompt — and it only takes effect once "Guidance
  strength" (`cfg`) is at least ~2.5. **This is NOT the same as "inert at t2i's default"**: t2i's own
  default `cfg` is 3.0 (see SETTINGS below), already above that line, so Things to avoid DOES work
  at t2i's default settings. It goes inert only at a *low* `cfg` — measured **zero** effect (max
  pixel delta 0) specifically at the `fast-plain` preset's `cfg` of 1, not at the default. —
  observed, `engines/qwen_image.py` fields (`negative` hint, `cfg` default 3.0 for t2i) + presets
  `fast-plain` note (measured 2026-09-23/22).
- **`edit` mode**: describe the change as a **positive instruction** anchored on what the uploaded
  picture(s) actually show — "Remove the background" works directly as a prompt, no separate
  before/after framing needed. — observed, `engines/qwen_image.py` `prompt_guides.edit`.
- **`pixelart` mode**: prompt the subject only, never the pixel style — "8-bit", "pixel art style",
  "16-color palette" in the prompt text does nothing; colour count and size are separate fields
  (`pixel_colors`/`pixel_size`) applied after the render, not part of the model's own job. —
  observed, `engines/pixelart.py` `prompt_guides.pixelart`.
- **Multiple reference pictures are wired by upload order**, not by name: the first picture added
  becomes reference 1, the second reference 2, and so on up to 10. When a user talks about several
  uploaded pictures by number ("the man in picture two", "picture 1 / picture 2"), write the edit
  prompt using that same ordinal language ("the first picture" / "the second picture") — this app
  does not use bracket tokens like `<image1>` in the prompt text itself, only positional image
  inputs. — observed (the wiring), `engines/qwen_image.py` `qwen_edit_graph()` (`images.image_%d`);
  **inferred, UNTESTED against a real render** (the ordinal-language convention as the right way to
  address them in prose) — no `prompt_guide` states an addressing convention; this is read off the
  graph's positional wiring plus a real-world request shape observed in a scouted Qwen-Image 2.1
  user's own workflow ("make the man in image two have the clothes of the man in picture one").
  Upstream's *own* fine-tuned prompt-rewriter (a different, specially-trained checkpoint this app
  does not run) uses literal `<image1>`/`<image2>` tags in its rewritten output — restated in my own
  words, not copied, source https://github.com/QwenLM/Qwen-Image-2.1/tree/main/prompt_rewrite (Qwen
  Research License; craft idea only). Treat ordinal language as the safer default, but this whole
  convention has never been checked against a real Qwen-Image 2.1 edit render — only against what a
  chat model writes as a prompt, not against what the engine actually does with it.
- **Named subjects the model doesn't recognise render wrong — describe appearance, not just the
  name.** A live BWF render (job `c78e51eab415`, 2026-09-24, prompt "...battle between Godzilla and
  MechaKing Ghidorah..."), inspected at full resolution: **three creatures** came back instead of
  two — two Godzilla-like reptilian kaiju (one with blue dorsal spines, one without, each firing its
  own energy beam) plus a **single-headed winged dragon with golden horns**, also firing a beam.
  MechaKing Ghidorah's actual defining features — three heads, gold colour, mechanical armour —
  never appeared on any of them; the engine had no reliable grip on the name, produced an extra,
  unrequested monster, and substituted a generic "dragon" reading for the one that was meant to be
  MechaKing Ghidorah. Fix: describe the unfamiliar subject's actual appearance directly ("a golden
  three-headed mechanical dragon with bat wings and armour plating") rather than leaning on the
  name, and state every count explicitly. — observed, live BWF render `c78e51eab415`, inspected
  directly at full resolution, 2026-09-24, in the guide's live vision probes.
- **Identity from a reference image should be pointed at, not re-described in words** — restating a
  person or character's features in prose, instead of just pointing at the uploaded reference, tends
  to regenerate and degrade the likeness rather than hold it. — **inferred**, restated from the idea
  in Qwen-Image 2.1's own upstream edit-prompt rewriter (Qwen Research License; craft idea only, not
  copied text), same GitHub source above. Apply cautiously — not independently measured against this
  app's own edit graph.
- **How Qwen-Image 2.1's own upstream rewriter structures a good description** (the app doesn't run
  this exact tool — it needs a different, specially fine-tuned checkpoint — but its structure is the
  clearest picture of what this model responds to; restated in my own words, never copied, same
  GitHub source, Qwen Research License):
  - For `t2i`: open with one ~20-word sentence naming medium, style, subject, background; walk the
    frame region by region (or, for a single centred subject, walk the subject — background, pose,
    face, body/garments, edges); give legible text its own sentence with exact wording in quotes;
    give lighting its own sentence (source, direction, quality, shadows); close with one sentence
    stepping back to the whole composition. Stay observational — present tense, third person, no
    "you"/"create", no quality-booster words like "masterpiece" or "8K". Name colours with a
    modifier ("deep navy", not "blue") and materials, not just nouns ("brushed metal", not "metal").
  - For `edit`: the core discipline is naming exactly what changes and holding everything else fixed
    by describing it at a high level rather than repainting it in detail — over-describing something
    meant to stay the same makes a diffusion model more likely to drift it, not less. Lead with the
    operation as an instruction, not a description of the finished picture. Commit to exact
    characters for any text that will appear in the output.
  - Both apply cleanly as general Qwen-Image 2.1 craft even though this app's own `prompt_guides`
    (positive-only for `t2i`, plain instruction for `edit`) are shorter — these upstream ideas
    explain *why* those short rules work, without contradicting them.
- **Vocabulary that transfers from other image models — inferred, cite before using.** The maintainer's
  NotebookLM research notebook ("AI Artistry: Transforming Photos with Gemini and Meta", 29 sources) is
  almost entirely about **Nano Banana / Gemini**, a different model family. The craft vocabulary
  below is a reasonable generalisation for a Qwen-Image 2.1 prompt, but the exact prompt *syntax*
  those sources use (JSON-structured prompts, Gemini-specific phrasing) is NOT verified against
  Qwen-Image 2.1 — offer the vocabulary, cite where it's from, never claim it as Qwen-specific fact:
  - Camera angle / framing: full body shot, close up, extreme close up (pore/eyelash detail),
    extreme long shot (a tiny figure in a vast landscape), high angle (looking down, reads
    smaller/vulnerable), bird's-eye view, Dutch angle (tilted, tense), side profile
    (introspective), low angle (reads powerful), over-the-shoulder, off-centre framing, shot from
    behind, POV. — inferred, https://imaginewithrashid.com/gemini-nano-banana-pro-prompts-for-camera-angles/
    (via the maintainer's notebook).
  - Lens: 35mm (natural/editorial), 50mm (standard), 85mm (portrait compression), 85–100mm
    (telephoto hero shot), wide-angle, macro, anamorphic (cinematic, oval bokeh), fisheye, shallow
    depth of field (creamy bokeh), deep focus. — inferred, same notebook.
  - Lighting: soft diffused light, three-point softbox, soft key + rim light, direct on-camera
    flash (harsh, retro), high-key (bright, minimal shadow), low-key (dark, deep shadow),
    volumetric light, ambient/neon glow. — inferred, same notebook.
  - Composition: depth stratification (foreground/midground/background), centred vs off-centre
    framing, safe margins/negative space, isometric perspective. — inferred, same notebook.
  - A useful first-draft shape — subject + adjectives, action, setting, camera angle/framing,
    lighting/atmosphere, style/medium — matches the order Qwen-Image 2.1's own rewriter uses (above),
    a genuinely convergent pattern, not just borrowed dressing. — inferred (Nano Banana source)
    converging with observed (Qwen's own rewriter structure).

## SETTINGS — the fields and presets that change the render

- `t2i` fields: `prompt` (required), `width`/`height` (default 1328×1328, snapped to a multiple of
  16), `negative`, `steps` (default 20), `cfg` (default 3.0), `resolution` (encoder working
  resolution, default 1024), `sampler`, `scheduler`, `guidance_style` (`Balanced` default —
  APG+FreSca, holds exposure at higher guidance but costs ~3x the render time and can add
  unrequested props; `Plain` is classic guidance, faster, but darkens the image once `cfg` passes
  ~2.5). — observed, `engines/qwen_image.py` `fields.t2i`, `presets.t2i["default"]`,
  `["fast-plain"]`.
- `t2i` presets: `default` (Balanced, cfg 3), `fast-plain` (cfg 1, ~3x faster, negatives inert),
  `seamless-tile` (25 steps @ 1024², plain guidance — measured to beat higher-step/higher-res
  renders for tiling, which drift warm and worsen seams), `set-plate` (no-people negative, for a
  reference plate reused across shots — asymmetric framing matters more than the prompt saying "no
  people"), `character-anchor` (a face reference that must NOT be lit like a finished scene). —
  observed, `engines/qwen_image.py` `presets.t2i`.
- `edit` fields: same core set, but **no width/height** — output size comes from the uploaded
  picture via the encoder's own latent (`resolution`, default 1024, governs working size only).
  `cfg` defaults to 2.5 (vs 3.0 for `t2i`); `guidance_style` defaults to `Plain` (vs `Balanced` for
  `t2i`) — a deliberate, still-open app choice: the Balanced arm was only ever measured on `t2i`,
  never benchmarked against edit. — observed, `engines/qwen_image.py` `fields.edit` (`ref_images`,
  `resolution`, `cfg` default), code comment on `guidance_style` default.
- `edit` presets: `default` (resolution 1024, steps 20, cfg 2.5), `remove-background` (works, but
  ~10x slower than Clean-up's dedicated `cutout` — steer bulk work there instead). — observed,
  `engines/qwen_image.py` `presets.edit`.
- Both `t2i` and `edit` share the same four-step quality ladder: Draft (12 steps) → Standard (20,
  default) → High (30) → Max (40). — observed, `engines/qwen_image.py` `quality.t2i`,
  `quality.edit`.
- `pixelart` fields: `prompt`, optional `shape_image` (a black silhouette on white — the sprite is
  drawn to fill that exact outline instead of a free pose), `negative`, `width`/`height` (only used
  without a shape image), `resolution`, `steps`, `cfg`, then the sprite-specific trio: `pixel_size`
  (default 64px final square), `pixel_colors` (default 8), `pixel_dither` (default 0 — flat crisp
  edges; a hard metal surface usually looks better with it off, raising it adds a fine dotted
  texture). — observed, `engines/pixelart.py` `fields.pixelart`.
- `shape_image` measurably matters: held-shape renders hit a median silhouette match of 0.92 against
  the target outline; a free render (no shape) only reaches 0.57 — push a user toward a shape image
  when they need a specific pose/silhouette (a game sprite that must fit a hitbox, say), rather than
  trying to describe the pose in words alone. — observed, `engines/pixelart.py` module docstring
  (measured 2026-09-22, 3 seeds).
- `pixelart` presets: `default` (8 colours, no dither), `richer-palette` (16 colours, for a busier
  subject). Same four-step quality ladder as Picture. — observed, `engines/pixelart.py`
  `presets.pixelart`, `quality.pixelart`.
- `cutout`/`upscale` have exactly one field each (`image_filename`, a picture) and no quality/speed
  axis to trade — "Standard" is the only setting because neither builder exposes a step count or a
  second checkpoint. — observed, `engines/cleanup.py` `fields`, `quality`.

## TAKE — what comes back, and its licence

- `pixelart` needs the app's Python deps (numpy, Pillow) installed on the machine running BWF, or
  the whole mode is unavailable regardless of which lane is picked — a core-side dependency, not a
  model file. — observed, `engines/pixelart.py` `_deps_reason()`.
- `upscale`'s RealESRGAN pass is wrap-aware, so a tileable texture's edges stay matched after
  upscaling. — observed, `engines/cleanup.py` `mode_notes.upscale`.
- Licensing differs by tool: Qwen-Image 2.1 (Picture, Pixel Art) is a **research licence,
  non-commercial**; Clean-up's BiRefNet is MIT and RealESRGAN is BSD-3-Clause, both freely
  shippable. — observed, `engines/qwen_image.py` `licence`, `engines/cleanup.py` `licence`.

## JUDGE/FIX — reading a finished render, "Not right? Tell the guide"

- When a render comes back wrong, name in one sentence what likely went wrong, tied to a real known
  failure mode (an unfamiliar/named subject, an unstated count, `negative` set but guidance too low
  to matter, no colour/material given), then return a revised prompt — or, if the picture already
  exists and only needs a targeted local change, point at Edit mode instead of a full re-roll. —
  observed, this app's own field/preset behaviour (above), applied as diagnostic method.
- **When nothing looks wrong, say so — don't manufacture a critique.** Measured directly on the
  cabin render (job `1b31fcf99d5a`, "a cozy cabin nestled in a dense, snow-covered forest during a
  vibrant sunset"), a clean render with nothing actually wrong with it: an early prompt draft of this
  guide invented problems ("the cabin looks swallowed by the trees", "the scale feels off") that
  weren't real defects — the render matched the prompt's own wording well. Fixed by adding an
  explicit instruction that an honest "this looks like what you asked for" is a real answer, and
  that a critique should only be named if it's something you'd actually point at on the screen. —
  observed, live-trial vision probe P5.
- **Before confirming a detail the user names, check it's actually visible.** Same trial: asked to
  read "the small text on the sign next to the cabin's door" (the cabin has no such sign at all —
  ground truth confirmed by direct inspection), an early draft answered as if a sign existed but was
  merely too small to read, instead of noting no such sign is visible. Fixed with an explicit rule to
  check presence before answering about a named detail. — observed, live-trial vision probe P6.
- **An exact count past two or three things is not reliable, even with a picture attached.** Directly
  measured: asked to count the monsters in the Godzilla/MechaKing Ghidorah render (ground truth: 3,
  confirmed by direct inspection at full resolution — two Godzilla-like kaiju plus a single-headed
  winged dragon, three beams converging), the guide's endpoint answered "four" on repeated tries,
  even after being told counting is unreliable. The count is still wrong, but closer to true than it
  first looked once the ground truth itself was corrected (an initial pass here had mistakenly read
  the render as showing only two monsters). Adding the
  caveat did not fix the count itself — it only made the guide honestly hedge it and point at the
  user's own look, which is the right behaviour given a real capability limit this endpoint has on
  this image. This is a vision-model capability limit, not a prompt-writing mistake, and is not
  something a system-prompt fix can reliably correct — treat any count past two or three as a rough
  read to double-check, never a settled fact. — observed, live-trial vision probes P2 (first pass and
  after two fix attempts).
- **Garbled or stylized rendered text should be flagged as uncertain, not confidently transcribed.**
  A render with a neon sign showing garbled cursive script (not real words) was, on an early pass,
  confidently transcribed as specific (wrong) text; the fixed guide correctly said it couldn't read
  the exact characters because the script is stylized, while still confirming the overall scene
  matched. — observed, live-trial vision probe P3.

## What I can't judge — say so, don't guess

- A specific real person's identity or likeness, from any render — always defer to the user's own
  look, pictures attached or not. — observed, hard rule carried from the film persona's own
  boundary, applied here since the risk (a person's face) is the same regardless of room.
- Fine per-pixel detail that needs a full-resolution crop — colour-accuracy, exact finger count,
  texture quality at native size — this app's helper channel only ever sees a resized copy of a
  render, not the original file. — inferred, consistent with how the app actually sends images to a
  helper endpoint (no full-resolution guarantee observed in the code).
- An exact count of more than two or three things in a busy frame — see JUDGE/FIX above; measured,
  not merely assumed.
- Whether small or stylized rendered text says exactly what it appears to — legible-looking text in
  a generated image is not guaranteed to be real characters, and a diffusion model frequently
  produces garbled glyphs that only look like words at a glance. — observed, live-trial probe P3.
- Whether a named detail (a sign, a label, a specific object) is present at all just because the user
  asked about it — a leading question does not confirm existence; check the frame first. — observed,
  live-trial probe P6.
- Anything that needs to be heard or measured rather than seen (an edit's actual effect on a format
  this text channel can't render, an audio track, a file's metadata).

## A short survey of paid tools — what they claim, what's borrowed, what's not

Read for calibration, not to copy: none of this is BWF's own measured behaviour, and none of it is
copied into any shipped file — restated and cited.

- **Midjourney's Describe** (image → text): uploads a picture and returns four candidate prompts
  describing it, explicitly framed as inspiration/discovery rather than an exact match — "the
  suggested prompts won't precisely copy your image." — observed (documented),
  https://docs.midjourney.com/hc/en-us/articles/32497889043981-Describe. **Not borrowed**: BWF's
  "Not right? Tell the guide" skill goes the other direction (render → diagnosis → fixed prompt for
  the *same* engine), not image → generic prompt suggestions for re-use elsewhere.
- **Ideogram's Magic Prompt** (text → richer text, automatic before generation): expands a short
  prompt, leaves a long, detailed prompt mostly alone ("tiny tweaks... review to be sure it hasn't
  added unwanted elements"). — observed (documented), https://docs.ideogram.ai/using-ideogram/generation-settings/magic-prompt.
  **Convergent, not borrowed**: this app's own upstream Qwen rewriter idea (see PROMPT above) makes
  the same move — a short brief still gets a full, detailed description, never a thin one — but
  Ideogram's own documented warning (an over-enhanced long prompt can drift with unwanted additions)
  is a genuine, citable caution worth carrying into this persona's own rules: never pad an
  already-complete, detailed user prompt with invented extras.
- **Leonardo AI's prompt-generation tooling**: mostly third-party GPT wrappers around Leonardo's own
  API rather than a first-party in-app assistant with clear official documentation — search turned
  up no first-party "PromptGen" feature with a citable spec. — observed (absence), search survey
  2026-09-24. Nothing to borrow or disconfirm here; flagged as unverified rather than asserted either
  way.
- **Krea's realtime "Enhance"**: a live, streaming canvas where every prompt/brushstroke change is
  reflected in the output as it renders, built for fast visual exploration rather than a one-shot
  final prompt. — observed (documented), https://docs.krea.ai/user-guide/features/realtime. **Not
  applicable to BWF's shape**: BWF's render loop is a discrete job (submit → poll → done), not a
  live stream, so this pattern doesn't transfer as a UI mechanic — but the underlying idea (show the
  user what's changing, immediately) is already covered by this persona's own "show the exact
  prompt before treating the job as done" rule, arrived at independently from this app's own
  design, not borrowed from Krea.
- **Net effect on this persona**: the one concrete, citable lesson worth keeping is Ideogram's own
  documented caution against over-enhancing an already-detailed prompt — folded into the Picture
  prompt writer's rules (`skills.md`) as "a short brief gets a fuller description; a detailed one
  gets left mostly alone, not padded with unrequested extras." Everything else surveyed either
  doesn't map onto BWF's job-based (not realtime) architecture, or converges with a rule already
  derived independently from this app's own engine code and the upstream Qwen rewriter.

## Research log

- Read directly, first (in-house before web, per the app's own request guard and engine packs):
  `engines/qwen_image.py`, `engines/pixelart.py`, `engines/cleanup.py`, `rooms.json` — this app's
  own repository.
- Read two in-house scout notes (internal project research, not public) that surfaced the request
  shapes and failure patterns: one on a Qwen-Image 2.1 user's own workflow (the "describe by
  appearance, not just name" and "picture 1/picture 2" request shapes) and one on another Qwen-Image
  2.1 studio's UI (the `@name` → `<imageN>` pattern, confirmed not currently built into BWF).
- Then the model's own card: Qwen-Image 2.1's Hugging Face page (licence, aspect ratios, resolution
  ceiling) — confirmed the app's own field defaults are inside the model's supported range.
- Then, restated only (per licence, never copied): the two Qwen-Image 2.1 upstream prompt-rewriter
  system prompts (t2i and edit) published at
  https://github.com/QwenLM/Qwen-Image-2.1/tree/main/prompt_rewrite — Qwen Research License
  Agreement, restated in my own words, cited to that GitHub source.
- Then general web craft: queried the maintainer's NotebookLM research notebook (29 sources, mostly Nano Banana/Gemini prompt collections)
  for camera-angle, lens and lighting vocabulary that generalises across image models — everything
  pulled from it is labelled inferred, since the source model differs from Qwen-Image 2.1 and none
  of it was trialled against this app's actual renders; and a short web survey of Midjourney's
  Describe, Ideogram's Magic Prompt, Leonardo AI's prompt tooling, and Krea's realtime Enhance for
  comparison (see the paid-tools section above).
- Then live measurement: inspected three real BWF renders directly (the Godzilla/MechaKing Ghidorah
  battle, a rain-soaked-alley portrait, a snow cabin), established ground truth by eye first, then
  probed the guide's own endpoint against each. This is
  what surfaced the count-reliability limit, the fabricated-critique risk, and the
  confirm-before-answering rule, none of which were visible from reading code alone.
- Could not confirm: whether Qwen-Image 2.1 (the base checkpoint this app runs, not the specially
  fine-tuned PE checkpoints) actually responds to bracket tokens like `<image1>`/`<image2>` in plain
  prompt text the way the upstream rewriter's *output* does — no render was trialled either way in
  this pass, chat-only by this pass's scope. Whether the guide's endpoint's count failure
  (measuring 2 large, well-separated subjects as 4) is specific to this 12B model or a broader small-
  VLM limitation was not tested against a second endpoint.
