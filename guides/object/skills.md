# Object Room Guide — skills

Two writing/behaviour jobs, both grounded in `knowledge.md`. Output shape for the brain
line-delimited, never multi-line JSON (small local models don't reliably escape newlines inside a
JSON string; see `PROVEN-EXPANDER.py`'s own reasoning). Each skill below gives: what field it fills,
where that field actually lives, the one question it asks when ambiguous, and a worked example.

## Skill 1 — "Write me a source picture"

**Trigger**: the user wants a picture made for the 3D room's `mesh` mode — "make a model of X",
"I need a picture of my [object] for this", or a direct "write me a prompt for the source picture."

**What it fills**: the **Picture room's** `t2i` mode `prompt` field (type `textarea`, id `prompt`) —
a DIFFERENT room (`picture`, capability `image`) than the one this guide lives in. The skill's job
ends at handing over the words; it never fills the field itself. State this every time, plainly:
"this goes in the Picture room's Prompt field — go there, paste it in, and press Make."

**The one question it asks when ambiguous**: if the user's description doesn't already make clear
what the object's most fragile feature is (a thin part, a reflective surface, something that could
be occluded), ask about that ONE feature — nothing else. If the description is already complete
enough to write a good prompt (an object with no obvious fragile feature, described plainly), ask
nothing and write the prompt.

**Rules the skill must follow, from `knowledge.md`**:
1. One object only, named explicitly, with "nothing else in frame."
2. The whole object, uncropped — say so in the prompt ("the whole [object] visible, not cropped").
3. A 3/4 view, named explicitly, not "front view" or no angle at all.
4. Even, flat/soft light, explicitly ruling out hard shadows or a bright hot spot.
5. A plain, higher-contrast-than-the-object background (BWF crops the picture automatically, but a
   background too close in colour to the object can still confuse that step) — say this once as a
   footnote, not as the main instruction, since the app does most of the background work already.
6. If the object has a documented fragile TRELLIS.2 failure mode (thin parts, transparency/
   reflectiveness, multiple/tangled pieces, heavy occlusion), name the specific risk and adjust the
   prompt for it (e.g. a chain "laid out, not tangled" rather than draped).

**Output shape** (line-delimited, no JSON):
```
FIELD: prompt
ROOM: picture (t2i mode)
VALUE: <the full prompt text, one paragraph, no line breaks>
NOTE: <one sentence — what to watch for when the photo comes back, if the object has a fragile part>
```

**Worked example**:

Topic in: "a leather work boot"

```
FIELD: prompt
ROOM: picture (t2i mode)
VALUE: A single worn leather work boot, laces intact, nothing else in frame, the whole boot visible from toe to top of the shaft, photographed at a three-quarter angle showing the toe and the outer side, even soft studio lighting with no hard shadow or bright highlight, plain light-grey background.
NOTE: laces are thin and can be lost in the mesh — if they come out fused or missing, try a version with the laces tucked in rather than loose.
```

## Skill 2 — "Not right? / Revise"

**Trigger**: the user shows a finished mesh or turntable result (a still, or describes one) and
says something is wrong — "the chain is missing", "it looks melted on one side", "the turn is
choppy", or a bare "this doesn't look right."

**What it fills**: nothing directly — this skill's output is a DIAGNOSIS plus either (a) a revised
source-picture prompt (same shape as Skill 1, for the Picture room) or (b) a named `turntable` field
change (for the 3D room's own `turntable` mode), never both framed as certain — pick the one the
complaint actually matches, from `knowledge.md`'s "NOT RIGHT?" table.

**The one question it asks when ambiguous**: if no still is attached AND the complaint alone doesn't
map cleanly to one row of the table (e.g. "it doesn't look right" with no detail), ask the user to
either attach a still or describe specifically what looks wrong — nothing else.

**Text-only fallback** (no still attached, or the channel can't carry images): ask the user to
describe what they see as plainly as they can — missing/thin part, doubled object, one side looking
wrong, a shadow that doesn't move, or the turn itself being choppy/slow — and diagnose from the
description using the same table. Say once that you're working from the description alone.

**Output shape**:
```
DIAGNOSIS: <one sentence — what the complaint most likely is, from the knowledge.md table>
FIX_LOCATION: source_picture | turntable_settings | cannot_tell
FIX:
<if source_picture: a revised prompt in Skill 1's VALUE/NOTE shape>
<if turntable_settings: FIELD: <real field name> ACTION: <raise/lower/change to> WHY: <one clause>>
<if cannot_tell: one sentence saying so, and what to look at directly instead>
```

**Worked example**:

Complaint in: "the turntable spin looks jerky and grainy, here's a still" (still shows visible
grain/noise on the surface)

```
DIAGNOSIS: too few render samples and/or frames for a smooth, clean turn.
FIX_LOCATION: turntable_settings
FIX:
FIELD: Render samples
ACTION: raise (try the High preset: 120 frames, 768px, 64 samples)
WHY: more samples cleans up grain; more frames smooths the spin itself — no new source picture needed
```
