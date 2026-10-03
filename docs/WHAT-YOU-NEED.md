# What each BWF feature needs

This page lists the opt-in features that depend on something outside the app, what you need for each, what works without it, and how to turn it on.

---

## First run (Setup page)

**What it does:** with no `config.json`, the app opens a Setup page that finds ComfyUI, shows what each room needs, and writes the settings file for you.

**What you need:** a ComfyUI that is already running (on this computer or another one you can reach). Python 3.8 or newer to start the app; on Windows, macOS or Linux you can double-click `start.bat`, `start.command` or run `./start.sh`.

**Without it:** copy `config.example.json` to `config.json` and edit it by hand; see the README.

---

## Talking Head — typed line (default)

**What it does:** Generates a talking-head video from a face picture and a text line; the model invents the speech.

**What you need:** An LTX video lane (transformer, CLIP, video VAE, audio VAE) discovered on a ComfyUI lane.

**Without it:** The Talking Head room still opens; you can enter a line and pick a face, but generation will report the missing model roles.

**How to turn it on:** Select the Talking Head room, choose a lane that has the LTX models, fill in Face and Line, press Make.


---

## Talking Head — "Your own recording"

**What it does:** Uses an audio file you provide (wav/mp3) instead of the typed line; the face speaks in your voice.

**What you need:** Same LTX video lane as above, plus an audio file to upload. The field `audio_slice` accepts any wav/mp3 you bring.

**Without it:** The typed-line path works normally; the recording field is optional and simply ignored when empty.

**How to turn it on:** In the Talking Head form, upload a file in the "Your own recording" field (replaces the Line field). Set Length to match the clip (seconds × 24 + 1).


---

## Audio-led Cutting Room (`audio_led`)

**What it does:** Makes the cut follow a master sound track — each video shot is generated against a slice of that master so picture locks to the audio.

**What you need:** A sequence with `audio_led: true` (opt-in checkbox in the Cutting Room) **and** a master sound — either a picked take on a sound-lane slot, or your own sound file imported via `POST /api/sequence/master` (wav/mp3/m4a/aac/flac/ogg/opus, max 200 MB). Also needs `ffmpeg` and `ffprobe` on PATH.

**Without it:** The sequence behaves as a classic cut — the Audio-led checkbox is off by default, the master is ignored, and shots are generated without an audio window.

**How to turn it on:** In the Cutting Room, check **Audio-led**. Pick a take on a sound shot, or click the master row to import your own sound file.


---

## Importing a sound file in the Cut bar (master) and start offset

**What it does:** Lets you supply your own sound file as the sequence master (overrides any picked sound shot) and choose where in that file the cut starts.

**What you need:** `ffmpeg`/`ffprobe` on PATH. A sound file in one of the accepted formats (wav/mp3/m4a/aac/flac/ogg/opus, ≤ 200 MB).

**Without it:** The master row in the Cut bar shows nothing; you can still cut using a picked sound shot as master (if Audio-led is on) or cut without a master (Audio-led off).

**How to turn it on:** With Audio-led enabled, click the master row in the Cut bar → choose a file → it uploads to `POST /api/sequence/master`. The "Start (s)" field sets the offset into that file where the cut begins (`set_master_start`); "Clear" removes it (`clear_master`).


---

## Making a song in the Music lane

**What it does:** Generates a full song (vocals + instrumental or instrumental only) from a text description.

**What you need:** A music model that discovery finds on your lane — either ACE-Step 1.5 (song mode) or MiniMax-Music3 (music mode). Discovery matches by filename rules (see `docs/MODELS.md` "Discovery match" and each model's table).

**Without it:** The Music room opens but shows "needs …" for the missing roles; no generation is possible until the required model files are present on a lane.

**How to turn it on:** Open the Music room, select a lane that has the required models discovered, describe the song, press Make.


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

**How to provide:** Install `ffmpeg` (which includes `ffprobe`) and ensure both are on `PATH` before starting the server. The check runs once, when the app starts.


---

## Speech source (optional text-to-speech)

**What it does:** turns a typed line into a `.wav` through any text-to-speech service that speaks the OpenAI-style `/audio/speech` API, and hands the result back like an upload, so it can be used as "Your own recording" or as the master sound of an audio-led sequence.

**What you need:** the address of such a service (yours or a hosted one), set in `config.json`:

```json
"speech": {"url": "https://tts.example.com/v1", "model": "tts-1", "voice": "alloy", "api_key_env": "SPEECH_API_KEY", "timeout": 120}
```

Only `url` is required. `api_key_env` is the *name* of an environment variable that holds a bearer token; the token itself never goes in the file.

**Without it:** nothing changes. Record your own voice, or use any other tool's `.wav`; both work with the features above and need no service.

**How to turn it on:** add the `speech` section and restart. `GET /api/speech/status` reports whether it is on; `POST /api/speech` with `{"lane", "text"}` returns the same file description `/api/upload` does.
