# API examples

Worked requests for an agent (or a script) driving Black Wire Forge over HTTP. The loop is the one in
`AGENTS.md` ("Make something and get the file"): submit, poll, get the file. Field ids for any mode come
from `GET /api/engines?lane=<lane id>` (the mode's `"fields"` list); never guess them.

## A song (ACE-Step, mode `song`)

Run on a real lane on 2026-09-30, exactly as written below (the lane id was different):

```
curl -s -X POST http://127.0.0.1:3998/api/generate -H 'Content-Type: application/json' \
  -d '{"lane": "local", "kind": "audio", "mode": "song",
       "tags": "warm folk, acoustic guitar, gentle male vocal",
       "lyrics": "[Verse]\nThe lighthouse keeps the morning\nThe tide comes home again",
       "duration": 30}'
```

Answer: `{"ok": true, "job": {"id": "<job id>", "status": "queued", ...}}`. Poll
`GET /api/jobs?limit=20` until that job's `"status"` is `"done"`; its `"outputs"` was
`[{"filename": "SONG_00030.mp3", "subfolder": "blackwire", "type": "output", "media": "audio"}]`.
Then (only with `-o` to a place the user named):

```
curl -s -o <their path>/song.mp3 \
  "http://127.0.0.1:3998/api/view?lane=local&filename=SONG_00030.mp3&subfolder=blackwire&type=output&dl=1"
```

That returned HTTP 200, `audio/mpeg`, 30.0 s long. Other `song` fields: `bpm`, `keyscale`,
`timesignature`, `language`, and two optional style packs.

## The other Music room modes

Same loop; only the mode and its fields change.

| Mode | Engine | Content fields |
|---|---|---|
| `song` | ACE-Step 1.5 | `tags`, `lyrics`, `duration` (seconds) |
| `music` | MiniMax-Music3 | `caption` (the three-section structured caption), `lyrics`, `seconds` |
| `yue2` | YuE2-3B | `style`, `lyrics`, `max_duration` |
| `sfx` | Stable Audio Open | `prompt`, `negative`, `seconds` |

What each engine expects in those fields (caption shape, section tags, duets, lengths) is in
`guides/sound/knowledge.md`: the same notes the app's Sound guide reads.
