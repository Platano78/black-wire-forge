# Object Room Guide — skills

Two writing/behaviour jobs, both grounded in `knowledge.md`. Output shape for the brain
line-delimited, never multi-line JSON (small local models don't reliably escape newlines inside a
JSON string). Each skill below gives: what field it fills,
where that field actually lives, the one question it asks when ambiguous, and a worked example.

## Skill 1 — "Write me a source picture"

**Trigger**: the user wants a picture made for the 3D room's `mesh` mode — "make a model of X",
"I need a picture of my [object] for this", or a direct "write me a prompt for the source picture."

**What it fills**: the **Picture room's** `t2i` mode `prompt` and `negative` fields — a DIFFERENT
room (`picture`, capability `image`) than the one this guide lives in. As built
(`engines/mesh3d.py` `writers.mesh`, the Source picture writer, with `"target": {"cap": "image",
"mode": "t2i"}`): the 3D room's picture mode has no prompt, so "Help me write this" sits under a topic
box ("What should the 3D model be of?"); the preview says "This goes to the Picture room", and
**Use these** switches to Picture / text-to-picture with the fields filled. The user presses Make
there, then brings the picture back to the 3D room.

**The one question it asks**: only when the request does not say what the object is ("my
character"), offering 3-4 objects. A fragile part (thin, glass/chrome, tangled) is not asked about:
the writer names it in NOTE as the thing to watch and shapes the prompt for it. (An earlier draft
asked about the fragile feature; a question there rarely changes the picture, and the NOTE still
warns the user.) Its check sends one retry when the prompt names no three-quarter view, no whole
object, no background, or holds a negation.

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

**Output shape** (line-delimited, no JSON), a question OR a draft:
```
QUESTION: <one short question>
OPTIONS: <2 to 5 choices separated by |>
```
```
NEGATIVE: <things to leave out: hard shadows, reflections, text, other objects>
NOTE: <the part most likely to come out wrong in 3D, or the choices made>
PROMPT: <the prompt, last>
```

**Worked example**:

Topic in: "a leather work boot"

```
NEGATIVE: hard shadows, reflections, text, other objects
NOTE: The laces are thin: if they come out fused or missing, try the boot with its laces tucked in.
PROMPT: A clean product photograph of one worn brown leather work boot, the whole boot in frame from the toe to the top of the shaft with space all round it, seen from a three-quarter view that shows the toe and the outer side. Dark brown oiled leather, a thick black rubber sole with a deep tread, tan laces tied in a neat bow. Soft, even studio light from all round. A plain light grey background.
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

**As built** (`engines/turntable.py` `revisers.turntable`, the Turntable fixer, on "Not right? Tell
the guide" under a finished turntable): the still is one frame of the turn (a clip's middle still);
the fixer never describes or judges a side the still does not show, and says so when the complaint
is about one. Its fix words replace edit/reroll: `picture` (a new source-picture PROMPT; "Use it in
the Picture room" fills Picture / text-to-picture), `settings` (a SETTINGS line, `id = value; ...`,
matched to the turntable's own fields and range-checked; "Use these settings" fills them), or
`none` (a person's likeness, the unseen side: advice only). The mesh mode's `.glb` result has no still the
server can show a brain, so the fixer sits on the turntable.

**Output shape**:
```
QUESTION: <only when the user has not said what is wrong>
DIAGNOSIS: <one sentence, from the knowledge.md table>
FIX: picture | settings | none
PROMPT: <picture: the whole new source-picture prompt; otherwise blank>
SETTINGS: <settings: frames = 120; samples = 64; otherwise blank>
NOTE: <one sentence, or blank>
TWEAK: <at most one statement, or blank>
```

**Worked example**:

Complaint in: "the turntable spin looks jerky and grainy" (the still shows grain on the surface)

```
DIAGNOSIS: The model itself looks whole; the grain comes from too few render samples and the jerk from too few frames for one full turn.
FIX: settings
PROMPT:
SETTINGS: frames = 120; samples = 64
NOTE: More frames smooth the turn and more samples clean the grain; no new picture is needed.
TWEAK:
```
