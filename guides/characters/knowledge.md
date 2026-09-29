# Characters room — what the guide knows

Measured on the room's own sheets, 2026-09-28, on a 16 GB card at 25 steps. Nothing here is borrowed
from another model's documentation.

## The room
- One mode: a character design sheet from ONE reference picture. Fields: Reference picture, Name,
  Sheet prompt (the guide fills it, the user edits it), Sheet size. No seed field, no style picker,
  no separate turnaround or sprite step (those are later work, not in the room).
- The sheet is one 3:2 landscape page: a title column (title, three identity rows, three colour
  swatches), a central hero pose, a turnaround (front, side, back), three action angles, three
  silhouettes, three expression heads (machine states for a robot, vehicle or object) and a 2 by 3
  grid of close-up details.

## Sizes
| size | about | time |
|---|---|---|
| Quick | 1 MP (1216x832) | 23 s |
| Balanced (default) | 3.4 MP (2272x1504) | 33 s |
| Large | 6 MP (3008x2016) | 62 s |

Large leaves about 1 GB of a 16 GB card free, and its labels came out WORSE than Balanced. Say these as
measurements on that card only.

## What goes wrong, and the one thing to change
- A label is misspelled: it is a rare or long word. "UPWARD GAZE" was misspelled 5 of 5 times; "LOOKING
  UP" was right. Shorten it in the Sheet prompt box to one or two common words.
- The title is misspelled: "The Rookie" came out wrong, "Rookie" right. Use a one-word Name.
- An accessory swaps sides between panels: the prompt never said which side. Add the side, from the
  figure's own left and right, to that identity lock in every place it appears.
- A colour drifts between panels: the identity lock names the colour once and loosely. Name it the same
  way every time it appears.
- The sheet is crowded or blurry at Large: try Balanced.
- The wrong character came back: check the Reference picture.

## The sheet prompt, in short
Ten numbered paragraphs: subject and style; layout; title column; hero; turnaround; three action
angles; three silhouettes; three expressions or states; detail grid; final rules. 900 to 1600 words.
Identity locks (4 to 7, each a colour plus a shape plus a position) return in the hero, the detail grid
and the final rules. A signature colour sequence of three or four colours, taken from the subject,
runs through the title bar, swatches and frames. Every visible label is 1 to 3 common words in
capitals, in double quotes. No pixel sizes, no file formats.
