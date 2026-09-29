# Characters skills — for the Characters Guide

One writing skill. Output shape is **line-delimited, never JSON with multi-line fields**: small
local models do not reliably escape newlines inside a JSON string. The reply is one of two shapes,
and the full method lives in the pack's own writer prompt (`engines/qwen_image.py`,
`CHARSHEET_WRITER_PROMPT`); this file records what the skill is for and how it is checked.

## Skill 1: Sheet writer

**Trigger**: the user has a Reference picture and a Name in the Characters room and presses "Help
me write this". The Sheet prompt box may be empty: the picture and the name are the request.

**Reads**: the attached reference picture (when the helper can see pictures), and the room line
in brackets, which carries the Name. A name missing from the room line is asked for once
(`QUESTION:`). No picture attached is answered before the brain is asked ("Add a picture of your
character first").

**Writes**: `PROMPT` (the ten numbered sections, one paragraph each, 900 to 1600 words) into the
form's Sheet prompt field, and an optional `NOTE` (what the guide decided the user did not say).
It never sends anything to Make; the user reads the draft, presses "Use these", edits it, and
presses Make.

**Method**: audit only what the picture shows; 4 to 7 identity locks (colour, shape, position,
and the side of the body from the figure's own left and right) that return in the hero, the detail
grid and the final rules; a 3 or 4 stop signature colour sequence taken from the subject; the fixed
3:2 layout (title column, hero, turnaround, three action angles, three silhouettes, three
expressions or machine states, a 2 by 3 detail grid); every visible label 1 to 3 common words in
double quotes.

**Measured lessons the method holds (2026-09-28)**: "LOOKING UP", never "UPWARD GAZE" (misspelled
5 of 5); a one-word title ("Rookie" was spelled right, "The Rookie" was not); a stated side for
every accessory.

**Checked on the draft, never at Make time**: exactly ten numbered sections in order; no "UPWARD
GAZE"; 700 to 2000 words; the name is on the sheet. A draft with problems is rewritten once, with
the problems named; if it still has them, it ships with them shown.
