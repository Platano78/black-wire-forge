# Black Wire Forge

**Your own AI media studio: songs, pictures, video and 3D, made on machines you own, with a guide in every room.**

A plain-language web interface for one or more ComfyUI instances. Say what you want, choose an
available machine, and the app builds and submits the graph; sequence and process-lane workflows
can select an eligible machine automatically. **Black Wire Studios.**

Forked from [2Wild-Image-Video-Gen-Studio](https://github.com/tonyd2wild/2Wild-Image-Video-Gen-Studio)
(MIT).

## What's in it

- **Rooms.** Sound: Music, Cover, Sound FX, Producer. Picture: Picture, Characters (a design sheet
  from one picture of a character), Pixel Art, Clean-up, Textures (no built-in engine makes textures
  yet). Motion: Video, Talking Head. Object: 3D. Most rooms come with a guide (below).
- **A music video in one click.** On a finished song, "Make a music video from this song"
  plans shots of about 4 seconds, makes a picture of the person for each (from a photo of someone who
  agrees to be in it, or someone it makes up), animates each picture to its slice of the song and cuts
  them together. Pictures and video may come from different machines; the cut needs ffmpeg
  (`docs/WHAT-YOU-NEED.md`).
- **The Producer room** works on songs you already have, on this computer's processor rather than
  ComfyUI: **Grid check** (every beat and bar, with a click track to check them by ear), **Fit** (move a
  part onto another song's beats), **Re-arrange by bars** and **Mix** (up to four tracks, levelled).
  Setup: `docs/MODELS.md`.
- **The Cutting Room** puts what you made in order. Paste a script and each paragraph becomes a shot (a
  scene heading stays a heading); change a line and its shot is marked stale; cut the film with title
  cards (needs ffmpeg and a title font, `cut.fontfile`).
- **The Workflows tab** lists the templates your ComfyUI ships and the workflows you saved in it, each
  marked Ready, or with the model files, nodes or ComfyUI version it still needs, or saying it can't tell
  right now when that can't be checked. It installs and
  downloads nothing; "Save to ComfyUI and open" puts a copy of a template into ComfyUI.
- **What will run**, in every ComfyUI room: the exact model file each part of the mode loads, why it
  was picked, every other file that fits and any left out on purpose. Pin a file in `config.json` to
  choose it yourself.
- Also: style LoRAs from the Hugging Face Hub ("Browse styles"; downloading is off until you turn it on
  per machine), "Compare one setting" across several values, and your own modes as data packs (a ComfyUI
  workflow plus a small manifest, `docs/WRITING-A-PACK.md`).
- **An API your other apps and agents can call.** One JSON request makes something:

      curl -s -X POST http://127.0.0.1:3998/api/generate -H 'Content-Type: application/json' \
        -d '{"lane": "local", "kind": "image", "mode": "t2i", "prompt": "a lighthouse at sunset"}'

  `GET /api/engines?lane=<id>` describes every mode on a machine: whether its files are there, what it
  still needs and its fields (AGENTS.md lists the other checks). [`AGENTS.md`](AGENTS.md) is written for
  coding agents (set up, check, submit, poll, download); [`docs/API-EXAMPLES.md`](docs/API-EXAMPLES.md)
  has worked requests. There is no login, so keep the app on `localhost` unless you open it on a network
  you trust (`SECURITY.md`).

## The guides

Each group of rooms has its own guide: a chat persona that knows that room's craft and the engine
behind it. You tell it the idea in plain words, it asks a question only when the answer would change
the result, and it fills in the room's real fields. You can read and change everything it wrote
before you press Make.

- The Sound guide (Music, Cover, Sound FX) turns a topic into what the song engine needs: the style
  text, lyrics long enough for the length you asked for, or a description of a sound effect.
- The Picture guide (Picture, Pixel Art, Clean-up) writes the prompt for the right mode. On a
  finished picture, "Not right? Tell the guide" sends the picture and what's wrong, and it answers
  with a revised prompt or an edit to try.
- The Characters guide turns one reference picture and a name into a character sheet prompt that
  keeps the character the same in every panel.
- The Motion guide (Video, Talking Head) works one shot at a time: which engine, how to phrase the
  shot, and for a talking head, a line that fits the clip.
- The Object guide (3D) helps you pick a source picture that will turn into a good 3D model, and
  tells you plainly how the result came out.
- The Film Room guide (Cutting Room) turns an idea or a script into shots that cut together, one
  next step at a time.

The guides are optional and start switched off. With a guide off, the room works the same way: you
type into the fields yourself and press Make. Without a chat model connected, the guide panel still
shows that room's most important tips as plain text. To use the guides, point the app at any
OpenAI-compatible chat model, such as llama.cpp or Ollama on your own machine, as the `"helper"` in
`config.json` (see [Advanced configuration](#advanced-configuration)), then switch the guide on in
its panel. Compact suits small local models; Verbose gives the guide more of the room's knowledge.
The music video maker uses the same model to write each shot's scene.

## Who this is for

For people who already run ComfyUI on a GPU machine (or are about to set one up) and want a
plain-language front end for it. New to ComfyUI, or setting this up for someone else? Hand
this repo to an agentic coding assistant (Claude Code, Codex, Hermes Agent, ...) and have it
follow [`AGENTS.md`](AGENTS.md) — it walks through deciding whether you need ComfyUI at all,
installing, and verifying each piece is actually working.

### Easiest start

Double-click `start.bat` (Windows), `start.command` (macOS), or run `./start.sh` (Linux) to launch
Black Wire Forge. On first run the Setup page opens in your browser. To set a custom Python path or extra
arguments, copy `start-user.example.sh` (or `start-user.example.bat` on Windows) to the same name
without "example", next to the launcher, and edit it; the launcher itself never needs editing.
The launcher uses the first working `python3` or `python` it finds, not the repo's `.venv`; to use the venv
(for optional packages such as Pillow), set `PYTHON=.venv/bin/python` in `start-user.sh`.

Or from a terminal:

    test -e config.json || cp config.example.json config.json   # then put your own hosts in it
    python3 server.py                                            # http://127.0.0.1:3998

Runs in the foreground; leave it running and use a second terminal for anything else.
Or skip the copy: started with no `config.json`, the app opens a Setup page at
http://127.0.0.1:3998 (this computer only) that finds ComfyUI, shows the file and writes it.
If you tell it ComfyUI runs on the same computer and where its models folder is, each room can
download its model files there in one click (sizes and licences shown first; set `HF_TOKEN` for
gated repos); otherwise it shows an `hf download` command per file (`hf` comes with
`huggingface_hub`: install it into the app's own environment, `.venv/bin/python -m pip install
huggingface_hub`, or on Windows `.venv\Scripts\python -m pip install huggingface_hub`).
`requirements.txt` is optional (see "Install" below) and, on modern distro Python, needs its
own virtual environment rather than a global `pip install` — same command either way.

### On Windows

Install Python 3.8 or newer from python.org (tick "Add python.exe to PATH") and double-click `start.bat`. Or, in PowerShell in this folder:

    py -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
    .venv\Scripts\python server.py

Checked on Windows 11 with Python 3.13. For the Cutting Room, install ffmpeg and put it on your PATH; Grid check and the 3D turntable need their own programs, as on any system (see `docs/WHAT-YOU-NEED.md`). To run the tests, use Git Bash: `bash scripts/run-tests.sh`.

`config.json` is gitignored: it carries machine addresses and never belongs in the repo. The
`test -e ... ||` guard above only creates it if it doesn't already exist, so re-running this
never clobbers a real one.

## What it looks like

Each clip is a short loop; click one to download its full video (mp4). Long renders are cut, with a caption
saying how long they took; nothing is sped up.

| | |
|---|---|
| [![First run: the Setup page finds ComfyUI and asks what you want to make first](docs/images/v0-setup.loop.webp)](docs/media/v0-setup.mp4) | [![A song: tell the Music room's guide what you want, press Make, play it](docs/images/v1-song.loop.webp)](docs/media/v1-song.mp4) |
| **First run.** With no settings file yet, Black Wire Forge opens its Setup page: it finds ComfyUI, shows what each room needs, switches on a guide and writes the file. | **A song.** Switch the Music room's guide on and tell it what you want; it writes the style and the lyrics; you press Make and play it. |
| [![A picture: the guide writes the prompt, What will run shows the files, then a download with the recipe removed](docs/images/v2-picture.loop.webp)](docs/media/v2-picture.mp4) | [![A music video in one click: a made-up singer, the song cut into shots, the finished video playing](docs/images/v6-music-video.loop.webp)](docs/media/v6-music-video.mp4) |
| **A picture.** One box to the guide, a look at What will run, Make, then a download with the recipe removed. | **A music video in one click.** From that song: Make someone up, Start, and it plans, pictures and animates ten shots and cuts them to the song. |
| [![From a script to a cut: shots made from a storyboard, one re-made, then the cut plays](docs/images/v4-storyboard.loop.webp)](docs/media/v4-storyboard.mp4) | [![The Producer room: Grid check on the song, its summary and click track](docs/images/v7-grid-check.loop.webp)](docs/media/v7-grid-check.mp4) |
| **From a script to a cut.** Paste a script, make its shots, re-make one after a change, cut the film. | **The Producer room.** Grid check takes the song straight from History and finds its beats on this computer's processor: a summary line and a click track. |
| [![The Workflows tab: your ComfyUI's audio templates, with what each still needs](docs/images/v5-workflows.loop.webp)](docs/media/v5-workflows.mp4) | |
| **Workflows.** The templates your ComfyUI ships, each marked Ready or with the files it still needs and where they go. | |

<p float="left">
  <img src="docs/images/picture-result.png" width="49%" alt="The Picture room: a finished painting of a lighthouse, with the model, seed, time and licence stamp under it and What will run on the right.">
  <img src="docs/images/what-will-run.png" width="49%" alt="What will run in the Picture room: the model, text encoder and decoder files it loads, each AUTO PICK, with the model's part opened to show a Turbo file left out on purpose and why.">
</p>
<p float="left">
  <img src="docs/images/cutting-room-1440x900.png" width="74%" alt="The Cutting Room: the storyboard The Lighthouse Keeper, its script beats and three shots on the timeline, with the music video's sequence in the list above.">
  <img src="docs/images/cutting-room-390x844.png" width="24%" alt="The Cutting Room on a phone.">
</p>
<p float="left">
  <img src="docs/images/workflows.png" width="49%" alt="The Workflows tab filtered to audio: template cards, each marked Ready or with how many model files it still needs.">
  <img src="docs/images/setup-rooms.png" width="49%" alt="Setup, step 2: each room with its model files' total size, licences and an Installed badge checked against the ComfyUI it found.">
</p>
<p float="left">
  <img src="docs/images/picture-390x844.png" width="32%" alt="The Picture room on a phone, with the lighthouse picture open.">
  <img src="docs/images/video-390x844.png" width="32%" alt="The Video room on a phone, with a shot open.">
</p>

<details>
<summary>Every room (screenshots, 1440x900)</summary>

| | |
|---|---|
| ![Music: the song from the song clip open in the monitor, with its model, seed and licence stamp](docs/images/rooms/01-music.png) | ![Cover, empty: it asks for a song to add and how it should sound now](docs/images/rooms/02-cover.png) |
| **Music** | **Cover** |
| ![Sound FX, empty, with What will run showing the Stable Audio Open files it loads](docs/images/rooms/03-sfx.png) | ![Producer, nothing open: its four modes listed (Grid check, Fit, Re-arrange by bars, Mix) and the song field](docs/images/rooms/04-producer.png) |
| **Sound FX** | **Producer** |
| ![Picture: the lighthouse painting open, with What will run on the right](docs/images/rooms/05-picture.png) | ![Characters, empty: it asks for a picture of a character and a name](docs/images/rooms/06-characters.png) |
| **Picture** | **Characters** |
| ![Pixel Art, empty: the prompt, the optional shape to hold and What will run](docs/images/rooms/07-pixelart.png) | ![Clean-up, empty: remove a background or make a picture 4 times bigger](docs/images/rooms/08-cleanup.png) |
| **Pixel Art** | **Clean-up** |
| ![Textures: no built-in engine makes textures yet, and the room says so](docs/images/rooms/09-textures.png) | ![Video: one shot of the music video open, with History listing the run's shots](docs/images/rooms/10-video.png) |
| **Textures** | **Video** |
| ![Talking Head, empty: a face picture, the line to speak, or your own recording](docs/images/rooms/11-talking.png) | ![3D, empty: it asks for a picture, with What will run showing the TRELLIS2 files](docs/images/rooms/12-3d.png) |
| **Talking Head** | **3D** |
| ![The Cutting Room with the storyboard The Lighthouse Keeper open: script beats and three shots](docs/images/rooms/13-cutting.png) | |
| **Cutting Room** | |

</details>

The generated pictures, video frames and sounds in this section were made with Black Wire Forge, except the
ComfyUI template thumbnails in the Workflows clip and screenshot (see the credits); which
model made which is listed under [Credits for the media in this README](#credits-for-the-media-in-this-readme).

The app has no login. It checks that requests arrive through its own address (`localhost`,
the machine's own IP and hostname work with no configuration; any other name you reach it
by — a mDNS name, a reverse proxy — goes in `"allowed_hosts"`, e.g. `["forge.lan"]`; a proxy
must pass that `Host` with no port, or with the app's own port, since any other port is refused
before the name is checked) and
applies some defense in depth against cross-site browser requests. This is not
authentication and does not make the app safe to expose beyond a trusted LAN — see
`SECURITY.md` for exactly what the guard does and does not check.

## Install

The app runs with nothing beyond the Python standard library. `requirements.txt` (numpy,
Pillow) is optional: without it the server still starts and answers every request, and the
three things that need them -- the clean (recipe-removed) picture download, Pixel Art's
colour-lock/dither step, and the picture->video image-fit step -- report themselves
unavailable with a plain sentence saying what to install, instead of crashing the server. `ffmpeg` is likewise optional: without it the Cutting Room's
cut still opens, it just cannot build one yet (see the Cutting Room's own note in `/help`).
Install both in a virtual environment -- modern distro Python refuses a bare, global
`pip install` (PEP 668's "externally managed environment"; confirmed failing on a stock
Ubuntu 26.04/Python 3.14 box):

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python server.py

## Minimal config

`config.json` is required to use the app and its `"lanes"` list must not be empty -- the app
ships no machine addresses of its own. With no `config.json` at all, `python3 server.py` starts
in Setup mode instead: open http://127.0.0.1:3998 on the same computer and it asks where
ComfyUI (and, optionally, a guide) is, shows the file, and writes it -- never over an existing
one. A `config.json` that exists but is broken still stops the app with a message. The smallest
working config, one ComfyUI instance running on the same machine:

```json
{
  "port": 3998,
  "bind": "127.0.0.1",
  "lanes": [
    {"id": "local", "name": "This machine", "host": "127.0.0.1", "port": 8188, "caps": ["image"]}
  ]
}
```

- **lane**: one ComfyUI instance the app can send a job to. Each needs an `id` (unique),
  `name` (shown in the UI), `host`/`port` (where its own ComfyUI API answers). `caps` --
  which kinds of job it's for (`"image"`, `"video"`, `"audio"`, `"3d"`, `"producer"`) -- is *optional* for
  a ComfyUI lane: leave it out and the lane is considered for every capability, and
  discovery decides what it can actually run from the model files it finds. Set `caps` to
  a non-empty subset to restrict the lane's purpose on purpose. A **process** lane (a local
  program instead of ComfyUI, see below) must declare `caps` -- nothing about it is
  discoverable until its programs are.
- **port** / **bind**: where this app itself listens. The example above binds only to
  `127.0.0.1` (localhost); change `bind` to `"0.0.0.0"` only when you intentionally want
  this reachable from other machines on your LAN -- the app has no authentication or TLS,
  so anyone who can reach it can see every job and start new ones (see `SECURITY.md`).
- **allowed_hosts**: extra hostnames the app will answer besides `localhost` and its own
  IP/hostname (see above).

`config.example.json` ships this same `local` lane on localhost (plus a `title`, an empty
`allowed_hosts` and a `note`). See "Advanced
configuration" below for multi-lane, multi-GPU, process-lane, status-tile and helper setups.

## Prerequisites for your first render

Black Wire Forge does not install ComfyUI, custom nodes, or model weights -- it only talks
to a ComfyUI instance you already have running. Start the app and open a room; a lane
missing something says so with a "needs ..." sentence rather than failing silently.

For the Picture room (Qwen-Image 2.1, `engines/qwen_image.py`), your lane's ComfyUI needs:

- **Node types**: `UNETLoader`, `CLIPLoader`, `VAELoader`, `TextEncodeQwenImage21`, `APG`,
  `FreSca`, `KSampler`, `VAEDecode`, `SaveImage` -- these are all core in current ComfyUI,
  nothing extra to add. The recommended GGUF-quantised UNet additionally needs `UnetLoaderGGUF`
  from the ComfyUI-GGUF custom node pack (see `docs/MODELS.md`'s "Custom-node packages").
- **Model files**: a UNet, a CLIP/text-encoder, and a VAE. The UNet and VAE filenames must match
  the `qwen_image` rules; the text encoder must match `qwen3vl` (preferably `8b`). See the model
  table in `docs/MODELS.md` for the exact filenames, and "Qwen-Image 2.1 preference" below.

Other rooms (Video, Music, 3D, ...) need their own engine's nodes and models the same way;
read the relevant file under `engines/` for the exact roles it declares.

## GPU and no-GPU paths

Most generation modes require a suitably configured ComfyUI GPU lane. Two things run without a
GPU, each on a `"kind": "process"` lane (see "Advanced configuration" below): turning an existing
`.glb` file into an orbiting turntable video, using Blender (rendered on the CPU,
`engines/turntable.py`) and `ffmpeg`; and the Producer room: Grid check, Fit and Re-arrange find a
song's beats and bars with beat_this, and Mix needs only `ffmpeg` (`engines/producer.py`; setup and
downloads in `docs/MODELS.md`). No
cloud-backed generation lane is included.

## Advanced configuration

Everything below is optional; the minimal config above is a complete, working setup on its
own.

- **Multiple lanes / multi-GPU**: add more entries to `"lanes"`. `gpu`/`gpu_label` name the
  physical card a lane sits on (defaults to the lane's `host`); two lanes sharing a `gpu` key
  are treated as unable to hold weights at the same time, and the app frees one before
  dispatching to the other. A lane may also carry `"shared": "..."` as a plain note that
  something else also uses it.
- **Process lane**: `{"id": ..., "name": ..., "kind": "process", "caps": ["3d"]}` -- runs a
  local program instead of ComfyUI (no `host`/`port`). The included turntable pack needs
  Blender and `ffmpeg` on `PATH`. Grid check needs `"producer"` in `caps`, `ffmpeg`, and a
  Python with beat_this that BWF is pointed at (`BWF_PRODUCER_PYTHON`; see `docs/MODELS.md`).
- **Status-only tile**: `"status_only": {"name": ..., "host": ..., "port": ..., "path":
  "/v1/models", "gpu_label": ...}` -- a read-only strip tile (e.g. an LLM server), never
  dispatched to. Omit it and the tile does not appear.
- **helper**: `{"url": "http://host:port/v1", "model": "...", "timeout_s": 60}` -- an
  optional OpenAI-compatible chat endpoint for "Help me write this" / "Describe this
  picture", which hand the prompt box's words (or the picture) to the room's guide; its
  answer appears in the guide panel. Omit it entirely to hide those buttons. If configured,
  your text (and, for "Describe this picture", the image, when the helper can see) is sent
  to that endpoint -- see `SECURITY.md`.

  The same helper also powers each room's **guide**: a persona (the Sound, Picture, Motion and
  Object guides in their rooms, the Film Room Guide in the Cutting Room) that talks the idea
  through with you inside the room, from the files in `guides/`. Each turn tells the guide the
  room's selected mode and main field values, and that no picture is attached (the chat is
  text-only), so it never has to take your word for either. A **Compact / Verbose** toggle picks how much the guide knows; Compact is the
  default and suits small models, and if the helper reports a context too small for your
  choice the panel says so but never switches it for you. A reply cut short by its length
  limit is flagged, never silently trimmed. The conversation is kept per room (per sequence
  in the Cutting Room) in your browser and, so it survives a reload or another device, on the
  app's own machine under `data/guide_history/`; it is sent with each turn. With no `helper` configured the
  panel still shows the guide's plain guidance and how to add a helper. Guide chat waits up
  to `timeout_s` (120 seconds when unset), since a verbose guide is slow on a small box. The
  panel states how much context each choice needs (the guide plus 4,096 tokens for the
  conversation). The app reads the helper's context from llama.cpp's `/props`, else from
  `/models`, which on llama.cpp reports the model's TRAINING context rather than what the
  server was started with; set `"context": 16384` (a whole number of tokens) in `helper` to
  state it yourself, and that wins. A model that thinks before it answers can spend a guide
  reply's whole budget thinking and write nothing; the app then asks once more with four times
  the budget (at most 16,384 tokens), and if that is still empty it says so in the panel. Set
  `"max_tokens": 8192` (a whole number of tokens) in `helper` to give every guide reply at least
  that much room; it never lowers a guide's own budget.

  **The guide starts switched off**, so a helper alone never changes how a room works: the prompt box is the main field and Make sends
  what you type. A "Guide: on / off" switch in the guide's header turns it on (remembered per browser). To have it start on for everyone
  who has not chosen yet, add `"guide_default": "on"` inside `"helper"`.

  **"Not right? Tell the guide"** on a finished picture sends the picture, the prompt that
  made it and what you say is wrong to the helper, which answers with what went wrong and a
  revised prompt (or an edit to try). The picture is only sent when the helper can see
  pictures: the app reads that from `/models` (a `capabilities` list naming `multimodal` or
  `vision`), or set `"vision": true` or `false` in `helper` yourself, and that wins. A helper
  that cannot see is told so, and asks you to describe the problem. `"max_images"` (default 1)
  caps how many pictures one guide message may carry.
- **cut.fontfile**: `{"cut": {"fontfile": "/path/to/a/TrueType/font.ttf"}}` -- the font the
  cut's title cards use. Without it the app looks for a handful of common DejaVu/Liberation
  paths. A cut with no title cards still works either way; a cut that DOES include one is
  refused with a plain configuration sentence until a font is available, one way or the other.
- **timing**: `{"timing": {"poll_seconds": 4.0, "job_poll_seconds": 3.0, "http_timeout": 8.0,
  "free_settle_seconds": 2.0, "discover_seconds": 300.0}}` -- lane-status poll interval,
  active-job history poll interval, per-request HTTP timeout, how long to let a driver
  settle after freeing VRAM, and how often to re-read a lane's model list. Shown values are
  the built-in defaults; only set a key to change it.

## What is ours, and why

| change | why |
|---|---|
| Pillow replaces macOS `sips` | upstream's image-fit path could not run on Linux at all |
| Qwen-Image **2.1** graph schema | the graph is built for the 2.1 node contract (`TextEncodeQwenImage21`); it is not built to also speak the 2.0 schema |
| discovery finds **GGUF** unets | one lane holds 2.1 only as GGUF, invisible to `UNETLoader` |
| downloads are **sanitized** | a ComfyUI PNG can embed model filenames and your full prompt |

**Qwen-Image 2.1 preference, not enforcement.** Discovery's role rule for the image UNet/VAE
*prefers* filenames containing `2.1` but matches any file whose name contains `qwen_image`;
it does not require `2.1` in the name. If a lane holds another `qwen_image`-named file,
discovery may select that one, and the 2.1-specific graph will be built against it. Each room's
**What will run** panel shows the exact file every part loads, why it was picked, every other
file that fits and every file left out on purpose. To fix a file, pin it under `"models"` in
`config.json` (top level for every machine, or on one lane): the panel tags it CONFIG. A pin
naming a file the lane does not have makes the mode unavailable with a sentence saying so,
rather than failing inside ComfyUI; a pin the lane has runs even if its name does not fit the
part's name rule.

## Reading order

- `docs/ARCHITECTURE.md` — lanes, discovery, packs, rooms, jobs, sequences and the cut
- `docs/WRITING-A-PACK.md` — how to add an engine
- `docs/WHAT-YOU-NEED.md` — what each opt-in feature needs (external deps, models, ffmpeg, etc.)

## Testing

`scripts/run-tests.sh` runs `tests/test_*.py` one suite at a time, each with `TMPDIR` pointed at
a scratch directory on disk instead of the box's default `/tmp` — on some Linux setups `/tmp` is a
RAM-backed tmpfs, and a killed or timed-out suite otherwise leaves its media there. The scratch dir
is deleted after every suite regardless of outcome. Pass suite paths as args to run a subset,
`SKIP="test_storyboard*"` to skip by glob, `TIMEOUT=<secs>` (default 900) per suite, and
`SCRATCH_ROOT=` to change where scratch lands (default `~/.cache/bwf-tests`). GNU `timeout` enforces that
per-suite limit; without it (stock macOS has none) the runner falls back to `gtimeout`, else
runs with no time limit at all and prints one note saying so, rather than failing outright. After each suite
the runner also stops any `tests/fixtures/fake_comfy.py` that suite left running (matched by its own
scratch dir in `--store`, printing `reaped fake_comfy pid N`); on Linux/macOS the fixture itself also exits
within a couple of seconds once the process that started it is gone.
No `config.json` is required to run
the suite: every test either spins up its own scratch config (`tests/_scratch_config.py`, for the
ones that `exec_module` `server.py` directly) or never imports `server.py` as a running app at
all -- none of them read the one next to `server.py`.

The runner uses `.venv/bin/python` when the repo has a `.venv`, else `python3`. The full suite
needs, beyond the Python standard library: `requirements.txt` (numpy, Pillow),
`requirements-dev.txt` (playwright) plus `.venv/bin/python -m playwright install --with-deps
chromium`, and `ffmpeg`/`ffprobe` on `PATH`. In the venv from "Install" that is:

    .venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
    .venv/bin/python -m playwright install --with-deps chromium

Without any of these, the suites that need them print a plain `SKIP` reason and exit cleanly
rather than failing -- a clean clone with none of the optional deps installed still passes the
suites that do not need them.

## Credits for the media in this README

The generated media in "What it looks like" was made with the models and tools below, each under its own licence:

| Media | Made with | Licence |
|---|---|---|
| The song (the song clip; the sound of the music video and Producer clips) | ACE-Step 1.5 (ACE Studio and StepFun) | [MIT](https://github.com/ace-step/ACE-Step-1.5) |
| Pictures (the picture clip, the Picture screenshots; in the music video clip, the made-up singer and the picture each shot starts from) | Qwen-Image 2.1 (Qwen team, Alibaba) | [Qwen Research License](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE), research or evaluation use only |
| Video (the storyboard and music video clips, the Cutting Room and Video screenshots) | LTX-2.5 (Lightricks) | [LTX-2.x Community License](https://github.com/Lightricks/LTX-2/blob/main/LICENSE-2_x) |
| Beat and bar times (the Producer clip) | beat_this, `final0` checkpoint (CPJKU) | [MIT](https://github.com/CPJKU/beat_this) |
| Guide replies, and the music video's scene for each shot | the chat model (the helper) the guide was pointed at | its own licence |
| Template thumbnails in the Workflows clip and screenshot | ComfyUI's own workflow templates, shown as your ComfyUI lists them | theirs |

## Licences

This application (the web UI and the Python server in this repository) is MIT, upstream's —
see `LICENSE`.

Black Wire Forge ships **no model weights**. Each engine pack under `engines/` drives a model
the user downloads themselves, under that model's own licence — this repo neither holds nor
relicenses those terms. See **[`docs/MODELS.md`](docs/MODELS.md)** for every built-in engine's
model source, size, and licence (including the non-commercial and territory gates) before
downloading anything.

Every finished job carries a licence stamp for the engine that made it, and the Credits panel
in the app lists the installed engines' licences.
