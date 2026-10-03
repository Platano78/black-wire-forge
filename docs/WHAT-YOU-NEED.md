# What each BWF feature needs

This page lists the opt-in features that depend on something outside the app, what you need for each, what works without it, and how to turn it on. Every claim comes from the code or existing docs.

---

## Talking Head — typed line (default)

**What it does:** Generates a talking-head video from a face picture and a text line; the model invents the speech.

**What you need:** An LTX video lane (transformer, CLIP, video VAE, audio VAE) discovered on a ComfyUI lane.

**Without it:** The Talking Head room still opens; you can enter a line and pick a face, but generation will report the missing model roles.

**How to turn it on:** Select the Talking Head room, choose a lane that has the LTX models, fill in Face and Line, press Make.

*Confirmed from:* `engines/ltx.py` fields `face`, `line` (talking mode); `docs/MODELS.md` LTX-2.5 roles.

---

## Talking Head — "Your own recording"

**What it does:** Uses an audio file you provide (wav/mp3) instead of the typed line; the face speaks in your voice.

**What you need:** Same LTX video lane as above, plus an audio file to upload. The field `audio_slice` accepts any wav/mp3 you bring.

**Without it:** The typed-line path works normally; the recording field is optional and simply ignored when empty.

**How to turn it on:** In the Talking Head form, upload a file in the "Your own recording" field (replaces the Line field). Set Length to match the clip (seconds × 24 + 1).

*Confirmed from:* `engines/ltx.py:1291` field `audio_slice` (label "Your own recording (instead of a line)", type `audio`).

---

## Audio-led Cutting Room (`audio_led`)

**What it does:** Makes the cut follow a master sound track — each video shot is generated against a slice of that master so picture locks to the audio.

**What you need:** A sequence with `audio_led: true` (opt-in checkbox in the Cutting Room) **and** a master sound — either a picked take on a sound-lane slot, or your own sound file imported via `POST /api/sequence/master` (wav/mp3/m4a/aac/flac/ogg/opus, max 200 MB). Also needs `ffmpeg` and `ffprobe` on PATH.

**Without it:** The sequence behaves as a classic cut — the Audio-led checkbox is off by default, the master is ignored, and shots are generated without an audio window.

**How to turn it on:** In the Cutting Room, check **Audio-led**. Pick a take on a sound shot, or click the master row to import your own sound file.

*Confirmed from:* `docs/ARCHITECTURE.md` "Audio-led sequences (opt-in)"; `server.py:1776` `_op_set_audio_led`, `server.py:5150` `seq_master_import`, `server.py:5146` `MASTER_EXTS`, `server.py:5147` `MASTER_MAX_BYTES`.

---

## Importing a sound file in the Cut bar (master) and start offset

**What it does:** Lets you supply your own sound file as the sequence master (overrides any picked sound shot) and choose where in that file the cut starts.

**What you need:** `ffmpeg`/`ffprobe` on PATH. A sound file in one of the accepted formats (wav/mp3/m4a/aac/flac/ogg/opus, ≤ 200 MB).

**Without it:** The master row in the Cut bar shows nothing; you can still cut using a picked sound shot as master (if Audio-led is on) or cut without a master (Audio-led off).

**How to turn it on:** With Audio-led enabled, click the master row in the Cut bar → choose a file → it uploads to `POST /api/sequence/master`. The "Start (s)" field sets the offset into that file where the cut begins (`set_master_start`); "Clear" removes it (`clear_master`).

*Confirmed from:* `server.py:5150` `seq_master_import`; `index.html:4800` `masterControlsHTML`, `index.html:4832` `set_master_start`, `index.html:4830` `clear_master`.

---

## Making a song in the Music lane

**What it does:** Generates a full song (vocals + instrumental or instrumental only) from a text description.

**What you need:** A music model that discovery finds on your lane — either ACE-Step 1.5 (song mode) or MiniMax-Music3 (music mode). Discovery matches by filename rules (see `docs/MODELS.md` "Discovery match" and each model's table).

**Without it:** The Music room opens but shows "needs …" for the missing roles; no generation is possible until the required model files are present on a lane.

**How to turn it on:** Open the Music room, select a lane that has the required models discovered, describe the song, press Make.

*Confirmed from:* `docs/MODELS.md` sections "ACE-Step 1.5" and "MiniMax-Music3"; `engines/audio.py` modes `song` and `music`; `docs/ARCHITECTURE.md` "Discovery".

---

## ffmpeg / ffprobe

**Features that need them:**

| Feature | What happens when absent |
|---|---|
| Cutting a sequence (`POST /api/sequence/cut`) | Refused synchronously with plain sentence: "`ffmpeg` and/or `ffprobe` must be installed and on PATH to cut this sequence into one file." |
| Importing a master sound file (`POST /api/sequence/master`) | Same refusal — the endpoint checks `CAN_CUT` before accepting the file. |
| Audio-led video generation (`_audio_led_slice`) | Refused with the same sentence; the shot cannot cut its window from the master. |
| Guide "Not right? Tell the guide" on a video clip | The middle-frame still extraction (`_video_still`) raises `ValueError`: "To show the guide a clip, ffmpeg must be installed and on PATH (it takes one still frame from the middle)." |
| Help page Cutting Room note | States "Cutting needs `ffmpeg`/`ffprobe` installed." |

**How to provide:** Install `ffmpeg` (which includes `ffprobe`) and ensure both are on `PATH` before starting the server. The check runs once at process start (`server.py:255` `shutil.which`).

*Confirmed from:* `server.py:255` `FFMPEG_BIN`, `server.py:256` `FFPROBE_BIN`, `server.py:257` `_CUT_MISSING`, `server.py:261` `CUT_REASON`, `server.py:3579` `_video_still`, `server.py:5150` `seq_master_import`, `server.py:5115` `_audio_led_slice`, `server.py:5455` `seq_cut_start`, `help.html:261`.

---

## Speech source (optional text-to-speech) — **planned**

**Status:** Being added on branch `speech-source`. Will be off by default.

**What it will do:** Accept any OpenAI-style `/audio/speech` endpoint for text-to-speech, so a typed line can be turned into audio without recording your own voice.

**What you will need:** An OpenAI-compatible TTS endpoint URL (configured in `config.json` when the feature merges).

**Without it:** Recording your own voice (the "Your own recording" field in Talking Head) works today and needs no external service.

**How to turn it on:** Will be a config option once merged; the Talking Head form will offer a TTS choice alongside the typed line and your recording.

*This row is a placeholder and will be updated when the `speech-source` branch merges.*

---

*All claims above were confirmed by grepping the codebase and docs. Features not listed here either need nothing external or are not opt-in.*