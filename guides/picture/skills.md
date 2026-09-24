# Picture-group skills — for the Picture Guide

Two writing skills, per the design brief's B2 request shapes and the "Not right? Tell the
guide" pattern. Output shape: **line-delimited, never JSON with multi-line fields** — small local
models do not reliably escape newlines inside JSON strings, a lesson proven on this project's own
earlier song-prompt expander. Each skill below states its exact output lines, in order, one value
per line.

**Question placement, both skills (design ruling):** the one allowed question is asked BEFORE
writing anything else, and only when the answer changes the prompt materially — a turn that asks a
question delivers nothing else that turn. Once a complete prompt/instruction has been delivered
(this turn or already, nothing left materially ambiguous), never trail it with a question; offer at
most one optional tweak, phrased as a plain statement ("I could also try..."), never as a question
("would you like...?"). This replaces an earlier draft where the skill sometimes delivered a full
prompt and then still asked a question in the same reply — a real behaviour observed in this
guide's own live trial and corrected on review.

## Skill 1: Picture prompt writer

Covers both of research-note B2's request shapes: (a) a description → one precise prompt (t2i), and
(b) a multi-image edit naming pictures by number → the edit engine's own reference form (edit).

### Shape A — t2i: subjects + style + setting + action → one prompt

**Trigger**: the user describes something to make, with no picture uploaded yet, in the Picture
room's t2i mode. Pixel Art has its own writer: see Skill 3.

**Fields filled** (real ids from `engines/qwen_image.py` `fields.t2i`): `prompt` (required),
`negative` (only if the user names something to avoid), everything else left at the room's own
defaults unless the user states a size, quality tier, or preset by name.

**Rules the skill must follow**:
1. Positive prompt only — never write "no X" or "avoid X" into `prompt`; that goes in `negative`.
2. Any specific named character, franchise thing, or real person → describe its actual appearance in
   the prompt in addition to (or instead of, if the name is obscure) the bare name, and state every
   count of people/creatures explicitly. This is the single most consequential rule — see the
   Godzilla/MechaKing Ghidorah case in `knowledge.md`.
3. Roughly follow the upstream rewriter's shape (restated, not copied — see `knowledge.md`): one
   opening sentence naming medium/style/subject/background, walk the frame in order, one lighting
   sentence, one closing composition sentence. A short user brief does NOT mean a short prompt — an
   under-specified frame just means the skill invents the rest sensibly, not that it writes less.
4. Colours get a modifier ("deep navy" not "blue"); objects get a material ("brushed metal" not
   "metal"); observational register (present tense, third person, no "you"/"create").
5. Camera/lens/lighting vocabulary from `knowledge.md`'s inferred section is fine to reach for, but
   never claim it as confirmed Qwen-Image 2.1 behaviour — it's craft borrowed from other models.
6. A short brief gets a fuller, invented-but-sensible description (per the upstream rewriter's
   "a thin brief never buys a thin description" rule); a detailed, already-complete user prompt gets
   left mostly alone — restructure and clarify it, but don't pad it with unrequested extra elements
   it didn't ask for. This mirrors a documented caution from Ideogram's own Magic Prompt feature
   (see `knowledge.md`'s paid-tools survey): an already-detailed prompt risks drifting when a helper
   over-enhances it.

**Ambiguity → ONE question, asked BEFORE writing, never after.** Ask only when the answer changes
the prompt materially — e.g. the user's topic doesn't say how many of something, or names a subject
genuinely ambiguous between two readings (a "dragon" — western reptile or eastern serpent?). Never
ask about style/lighting/camera choices the skill can just decide and show. **A turn that asks a
question delivers ONLY the question — no `PROMPT` line that turn.** Once a complete prompt has been
delivered (this turn or a prior one, nothing left materially ambiguous), never follow it with a
question — at most one optional, non-question tweak line (a statement: "I could also try a warmer
palette if you want" — never "would you like a warmer palette?").

**Output shape (line-delimited)**:
```
QUESTION: <the one question, ONLY when something material is genuinely ambiguous — if this line is non-blank, stop here, no PROMPT this turn>
PROMPT: <the full positive prompt text — only when QUESTION above is blank>
NEGATIVE: <text, or blank if none>
NOTE: <one sentence — what was inferred or decided, if anything material>
TWEAK: <at most one optional suggestion, phrased as a statement, never a question, or blank>
```

**Worked example** — topic in → fields out (no ambiguity, so no QUESTION line at all):
User: "a lighthouse at sunset, kind of lonely feeling"
```
QUESTION:
PROMPT: A realistic photograph of a single white lighthouse standing on a rocky, wave-battered point at sunset, with the ocean spreading out on both sides toward the horizon. The lighthouse occupies the left third of the frame, its lamp room catching the last warm light. The sky fills the upper two-thirds, streaked with deep orange and violet cloud bands fading to dusky blue at the top. The sea below is dark teal-grey with scattered whitecaps, and a thin strip of wet dark rock is visible at the base of the lighthouse. There are no other structures, boats, or people in the frame. The lighting is a low, warm sunset glow from the right, casting a long soft shadow from the lighthouse across the rocks. The overall composition is quiet and spare, with a muted, melancholy palette of orange, violet and teal.
NEGATIVE:
NOTE: no people/boats specified, so I made the frame empty on purpose to match the lonely feeling you described.
TWEAK: I could add a distant boat or gull if you'd like a touch of life in the frame.
```

### Shape B — edit: multi-image reference naming → the edit engine's own form

**Trigger**: the user has uploaded 2+ pictures in Picture's edit mode and talks about them by number
or description ("the man in picture two", "make picture 1 look like picture 2's lighting").

**Fields filled**: `prompt` (required, the edit instruction), `ref_images` is already populated by
the app from what the user uploaded — the skill only writes `prompt` text, using the SAME ordinal
language ("the first picture", "the second picture") matching upload order, since that's how
`qwen_edit_graph()` actually wires them (see `knowledge.md`). **This convention is UNTESTED against
a real render** — confirmed only as how the graph wires reference images, never confirmed that
Qwen-Image 2.1's edit checkpoint actually resolves "the second picture" in prose the way it resolves
the positional `image_2` input; use it as the best available default, not a proven fact. Never
invent a naming scheme the app doesn't use (no `<image1>` bracket tags in the prompt text — that
belongs to a different, specially fine-tuned checkpoint this app doesn't run).

**Rules**:
1. Anchor every claim on what the picture(s) actually show, if visible this turn; otherwise take the
   user's own description as given, and say so.
2. Name exactly what changes; describe what stays fixed only by type/role, not by re-painting it in
   detail (over-describing something meant to stay the same risks drifting it).
3. Lead with the operation as an instruction, not a description of the finished picture.
4. If identity/likeness from a reference matters (a face, a specific product), point at "the picture"
   rather than re-describing the person's features in words.

**Ambiguity → ONE question, asked BEFORE writing, never after.** Which picture is the "canvas"
(whose composition/background survives) is the single most common ambiguity in a multi-image edit —
ask it when it isn't obvious from the user's own phrasing ("put X into Y" already tells you Y is the
canvas; "swap their clothes" does not). **When this question is needed, deliver ONLY the question
this turn, no `PROMPT` line.** Once a complete edit instruction has been delivered, never follow it
with a question — at most one optional, non-question tweak line.

**Output shape (line-delimited)**:
```
QUESTION: <the one question, ONLY when the canvas or another material choice is genuinely ambiguous — if non-blank, stop here, no PROMPT this turn>
PROMPT: <the edit instruction, addressing pictures by upload order — only when QUESTION above is blank>
NOTE: <one sentence, if anything material was inferred>
TWEAK: <at most one optional suggestion, phrased as a statement, never a question, or blank>
```

**Worked example** (canvas is obvious from phrasing — "make the man in picture two wear..." already
names picture two as the one whose man/setting survive — so no QUESTION line):
User uploads picture 1 (a man in a red jacket) and picture 2 (a man in a lab coat), says "make the
man in picture two wear the jacket from picture one."
```
QUESTION:
PROMPT: Replace the clothing worn by the man in the second picture with the red jacket worn by the man in the first picture, keeping the second picture's man, pose, background and everything else unchanged.
NOTE: treating the second picture as the canvas since its own man and setting should carry over.
TWEAK:
```

## Skill 2: "Not right? Tell the guide"

**Trigger**: the user has a finished render and says something's wrong with it, in any Picture-group
room.

**Needs vision** to look at the render directly. **With a text-only brain** (no vision this turn, or
the endpoint has none), say so plainly in the first line of the reply and ask the user to describe
the problem instead of guessing.

**Rules**:
1. Name in one sentence what likely went wrong, tied to a real, known failure mode from
   `knowledge.md` — an unknown/named subject the model doesn't reliably know, an unstated count that
   rendered ambiguously, `negative` set but Guidance strength too low for it to matter, no colour/
   material given so the model guessed, or (edit mode) the wrong picture treated as canvas.
2. Return a revised prompt using Skill 1's same rules and output shape.
3. If the picture already exists and only a targeted, local change is needed (not a different
   composition), say so and point at Edit mode instead of a full re-roll in t2i.
4. Never claim to have watched the render as video, or seen a defect that isn't in front of it this
   turn.

**Live case to build in and re-run at every live-trial pass** (observed, image inspected directly at
full resolution, job `c78e51eab415`, 2026-09-24): prompt "...battle between Godzilla and MechaKing
Ghidorah..." rendered THREE monsters instead of two — two Godzilla-like kaiju plus a generic
single-headed winged dragon — none of them showing every feature ("three heads," "gold,"
"mechanical") that "MechaKing Ghidorah" actually implies, because the engine didn't reliably know
the second name and produced an extra, unrequested monster on top of it. See `knowledge.md`'s PICTURE
— edit section for the full ground truth.

Same before/after rule as Skill 1: if a genuine ambiguity blocks the fix (e.g. the user's complaint
doesn't say enough to know what changed), ask that ONE question first and deliver nothing else this
turn. Once a diagnosis and fix are ready to deliver, never trail them with a question — at most one
optional, non-question tweak line.

**Output shape (line-delimited)**:
```
QUESTION: <the one question, ONLY when something material is genuinely ambiguous — if non-blank, stop here, nothing else this turn>
DIAGNOSIS: <one sentence naming what likely went wrong — only when QUESTION above is blank>
FIX: edit | reroll
PROMPT: <the revised prompt, if reroll; or the edit instruction, if edit>
NOTE: <one sentence, if anything material>
TWEAK: <at most one optional suggestion, phrased as a statement, never a question, or blank>
```

**Worked example** (the live case — the complaint is specific enough to act on directly, so no
QUESTION line):
User: "I asked for Godzilla fighting MechaKing Ghidorah and the second monster doesn't look right —
it's not gold, doesn't have three heads, and doesn't look mechanical at all."
```
QUESTION:
DIAGNOSIS: the model doesn't reliably know "MechaKing Ghidorah" by name, so the bare name in the prompt didn't produce the right monster.
FIX: reroll
PROMPT: A cinematic realistic scene of a battle between exactly two giant monsters on a ruined city street at night: one is a large grey reptilian kaiju with spiked dorsal plates, the other is a golden three-headed mechanical dragon with bat-like metal wings and armour plating, lightning arcing between its three heads. The kaiju is mid-roar on the left, the mechanical dragon looms on the right, rubble and fire in the foreground. The lighting is harsh orange fire-glow against a dark smoke-filled sky. The overall composition is high-contrast and chaotic, centered on the clash between the two creatures.
NOTE: described the second monster's appearance directly instead of relying on the name, and pinned the count to exactly two.
TWEAK:
```

## Skill 3: Pixel Art sprite writer

The Pixel Art room's writer (`engines/pixelart.py` `writers.pixelart`, the Sprite writer). The
picture is drawn once, its background cut away, then shrunk to a small sprite (64px, 8 colours by
default). The prompt's job is a SOURCE picture that survives that shrink.

**Trigger**: a topic in the Pixel Art room ("godzilla", "a sprite for my platformer hero").

**Fields filled**: `prompt`, plus `pixel_size` / `pixel_colors` only when the user states them.

**Rules the skill must follow**:
1. Open with "Pixel art sprite, retro video game style," then the subject.
2. One subject, the whole body in frame with space around it; never a close-up or crop.
3. Name the view: side view (platformer character, vehicle) or three-quarter view.
4. A bold silhouette: limbs, tail, wings or weapon held apart from the body.
5. Flat colours in a few large shapes, a thick dark outline, at most four main colours named.
6. A plain flat white background, nothing else in the picture (the cutout removes it).
7. Never cinematic or dramatic lighting, glow, photo or 3D-render words, depth of field, close-ups,
   fine texture. The check flags these at Make time: that detail turns to noise at 64px.
8. A named subject: keep the name and describe how it looks (Skill 1's rule 2). Godzilla → a huge
   upright bipedal reptile kaiju, charcoal grey body, jagged pale dorsal plates, a long thick tail.

**Questions that change the result** (one per reply, the first that applies, never when already
answered): what the subject is, when the request does not say; the pose or action; the view, when a
game is named but not side-on or top-down; 8 or 16 colours, for a subject that needs many colours.
After one answer it writes and names its own choices in NOTE.

**Output shape (line-delimited)**:
```
PIXEL_SIZE: <NONE or a whole number 16-256>
PIXEL_COLORS: <NONE or a whole number 2-32>
NOTE: <the choices the user did not make>
PROMPT: <the prompt, last>
```
