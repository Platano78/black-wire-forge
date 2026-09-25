# The Motion Room Guide's skills

Three writing/judging jobs, one per situation the Guide gets pulled into. Each is a pack skill
(`writers` / `revisers` in `engines/ltx.py` and `engines/minimax_h3.py`): the engine's own rules live
in the pack, next to its fields. "Help me write this" is a short conversation: the writer asks at most
one question per turn, with clickable OPTIONS where the choices are few, and only when the answer
changes the shot; after four answers it must write, naming its defaults in NOTE. What the user sees
before anything fills the form is the preview of the engine's own fields.

Output shape for any field values the Guide hands back: **line-delimited, never JSON with multi-line
fields** — small models do not reliably escape newlines inside JSON strings (measured on the
project's own local-model tooling). The multi-line field (the prompt, or the Talking Head line) is
the LAST line of the block, everything after its label to the end of the reply.

## Skill 1 — Shot writers (Video room)

**Job:** topic in, a prompt the chosen engine actually obeys out, plus the length when the user
gave a number of seconds. The Video room's engine is already chosen by the mode, so the writer
never asks which engine.

| Mode | Writer | Fills |
|---|---|---|
| `ltx` | Shot writer | `prompt`, `length`, `width`/`height` (only exact pixel sizes) |
| `ltx_loop` | Long take writer | `prompt`, `length`, `width`/`height` |
| `fl2va` | Shot writer | `prompt`, `length` |
| `ref2v` | Reference shot writer | `prompt`, `length` |
| `continue` | Carry-on writer | `prompt`, `length` (never `prev_video`: that is a cable) |

**LTX (`ltx`, `ltx_loop`):** ONE paragraph, present tense, in the order subject, action, camera,
setting, light, then (`ltx` only) the sound, with spoken words in quotes. At most two actions (a
third is dropped). One camera move or a still camera. Posture and gesture, never emotion labels. One
light source. Every named person gets an action and a place. Never token weights, brackets, tag
lists or quality words. `ltx_loop` has no sound at all, so its prompt says nothing about sound, and
suits steady, unbroken motion. A request for a tall or square video keeps the widescreen canvas and
says in NOTE that a tall or square frame comes out with distorted motion. `length` stays NONE unless
the request gives a number of seconds; then seconds × 24, up to the next 8n+1 (10 s → 241).

**MiniMax-H3 (`fl2va`, `ref2v`, `continue`):** one plain, positive-only paragraph: the style first
("Live-action, cinematic," unless the user names another), subject, action, camera as one of H3's
named moves in plain words (push in, pull out, pan, tilt, tracking shot, arc shot, static shot, a
slight shake) with a size and speed, setting and light, then the sound. Anything spoken goes INSIDE
the prompt in quotes, with who says it: H3 speaks it; there is no separate voice. `ref2v` names each
reference by what the user calls it ("the woman from the reference picture") and writes the words
itself when the request asks someone to speak without giving them. `length` stays NONE unless the
request gives seconds; then seconds × 24 within 124–362 (the app snaps it to H3's own grid).

**`continue`:** the prompt MUST describe the SAME shot carrying on — same subject, camera move,
framing, place and light, with "keeps / goes on / still" wording — never "cut to", a new angle, a
close-up of something else or a new scene. The delivered piece is about a second shorter than the
length, so a stated 8 s becomes 216 frames.

**Questions (the first that applies, and only these):**
- ACTION — only when nothing happens in the request at all ("a lighthouse"); any verb is an action.
- CAMERA — `ltx`, `ltx_loop`, `fl2va`: the request has an action but says nothing about the camera.
- WHO — `ref2v`: the request does not say what the references show (no OPTIONS: the writer cannot see them).
- NEW SHOT — `continue`: the request asks for a new angle, place or close-up. OPTIONS:
  `Carry on the same shot | Cut to a new shot`. Carry on keeps the framing and lets the wish happen
  inside it; Cut writes the new shot and says in NOTE that it belongs in "A video from a starting
  picture (and text)", because here it plays as a hard cut.
- WHAT — `continue`: only when the request names no subject at all ("keep going").

**The check** (also the Make-time guard): token weights or brackets in the prompt; while drafting,
a `length` that is not 8n+1 or a width/height not a multiple of 32 on LTX (at Make time the graph's
own refusal stands, since confirming cannot help); on `continue`, new-composition words ("cut to",
"new angle", "a close-up of", "in a close-up", "meanwhile", …), naming the words and where a new
shot belongs.

**Worked example (LTX, a question then a write):**
> Request: a paper boat on a stream → QUESTION: What should the boat do? OPTIONS: Drift slowly
> downstream | Spin in an eddy | Tip over a small fall
>
> Answer: Drift slowly downstream → NOTE: I chose a still camera low at the water's edge.
> PROMPT: A small white paper boat drifts slowly downstream on a calm, clear stream, turning gently as
> it goes. A still camera sits low at the water's edge. Smooth pebbles and green reeds line the
> banks, lit by soft afternoon sun from the left. Water trickles and burbles softly, a bird calls in
> the distance.

## Skill 2 — Line writer (Talking Head room)

**Job:** a topic, a situation or the exact words in; the `line`, a short `look` and a `length` that
fits the line out. The topic is enough: "a pirate telling a kid to go to bed" gets a line in that
pirate's voice.

**The writer writes the words; the app works the length out.** A small brain counts words
unreliably (live trial: a 14-word line came back at 257 frames), so the pack's `derive` sizes the
clip from the written line: **words ÷ 2.2 words/s × 1.15 cushion × 24 fps, rounded UP to the next
8n+1, never below the 97-frame default** (the one length measured on this hardware). 10 words → 129,
13 → 169, 14 → 177. One clip holds at most 28 words (361 frames, about 15 s, the field's own UI
ceiling). The brain never writes the length. A length the user states in the request or an answer
("make it 10 seconds", "97 frames") wins instead, as frames rounded up to 8n+1 (10 s → 241); words
inside quotes are the line, never a length.

**Rules:** `line` is only the spoken words (no quotes, name or stage directions); the user's exact
words stay unchanged unless they chose to shorten them, and a shortening is named in NOTE. A topic
gets one or two sentences, 8–20 words, spoken to the camera. `look` is light and framing only
("warm lamp light, close-up"), never what is said — it rides at the END of the sent prompt as a
`Shot:` note. `LINE: NONE` is a quiet listening shot.

**Questions:** WHAT (no topic at all: "make him talk", OPTIONS of topics); TONE (a topic with no
speaker, character or mood: OPTIONS such as warm, deadpan, excited, stern — a named speaker already
sets the voice); TOO LONG (the user's exact words run past 28: OPTIONS `Shorten it | Split it into
two clips`; split writes the first part and gives the rest in NOTE).

**The check** (also the Make-time guard): a line longer than the length holds → "Set Length to N
frames"; a line past what one clip holds → "Shorten it, or split it across two clips"; a shot note
that carries speech; while drafting, a length that is not 8n+1.

**Worked example:**
> Request: a tired barista telling the queue the espresso machine is broken
>
> LOOK: warm cafe light, close-up
> LINE: Sorry, folks, the espresso machine just died. Tea, anyone? It's on the house.
> → 12 words, so the app sets Length to 153 frames (about 6.4 s).

## Skill 3 — Clip fixer ("Not right? Tell the guide", Video room)

**Job:** a finished clip's middle still + the prompt that made it + the user's complaint in;
DIAGNOSIS, FIX, a revised PROMPT and at most one setting TWEAK out. Serves `ltx`, `ltx_loop`,
`fl2va`, `ref2v`, `continue`; the Talking Head has none yet (its words are a line, not a prompt).

**A still is not the clip.** From the still the fixer describes only what is in that one frame (who,
where, framing, light). It never claims camera movement, speed, sound, speech, lip sync or timing
from it, never says yes or no to any of those, and opens with "I can't judge motion or sound from one
still, so going by what you say:" when the complaint is about one of them (this guide's own vision
trial: only an absolute rule stopped a 12B model confirming a push-in that was not there). With no
picture it says it cannot see the clip. It never gives a likeness verdict: check the face at full
size.

**FIX is always reroll:** a clip is made again from the revised prompt, never edited in place (the
fixer declares `fixes: ["reroll"]`, so an "edit" reply is refused rather than offering an Edit
button that has nowhere to go).

**Known causes it names:** more than two actions (LTX drops extras); a person with nothing to do;
no camera stated; emotion labels; mixed light; a square or tall starting picture on LTX; token
weights; on H3, a continue prompt with a new angle/framing/action (hard cut — and "keeps / the same"
wording is right, keep it); a reference not named in the prompt; a generic reference face (TWEAK:
Reference image sizing to max); speech not in quotes; the quickest Fast tiers (TWEAK: Fast best or
Best).

**The ONE question:** only when the user names nothing ("it's not right"): "What looks or sounds
wrong: …?" — never a guessed cause.
