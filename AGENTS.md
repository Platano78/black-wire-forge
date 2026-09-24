# AGENTS.md

You are an agentic harness (Hermes Agent, Codex, Claude Code, or similar) setting up **Black
Wire Forge** on behalf of a user who does not necessarily know ComfyUI. Follow this file in
order. Every step below names the exact command to run and the exact field to check in its
output — do not guess a field name or an error sentence; if something here goes stale, the
source of truth is `server.py` and `engines/__init__.py`.

## What this is / is not

Black Wire Forge is a **web front end** for one or more ComfyUI instances. It:

- installs **no** ComfyUI, custom nodes, or model weights of its own — it only talks to a
  ComfyUI instance the user already has (or will have) running;
- has **no login** — see "Rules for the agent" and `SECURITY.md` before you consider binding
  it to anything but `127.0.0.1`.

## Decide the path with the user first

Ask, in this order, and stop at the first one that applies:

1. **They already run ComfyUI somewhere** (this machine or another on their network) → skip
   to "Install + start" and point a lane at that ComfyUI's `host`/`port` in `config.json`.
2. **They have no GPU at all** → only one path works with no GPU: turning an existing `.glb`
   file into an orbiting turntable video, via the Blender + `ffmpeg` **process lane**
   (`engines/turntable.py`, CPU-only). Every other room needs a GPU-backed ComfyUI lane. Do
   not attempt to make image/video/audio/3D generation work without one. Before continuing,
   check both binaries are actually on `PATH`:

   ```
   command -v blender && command -v ffmpeg
   ```

   If either is missing, install it first (Blender: https://www.blender.org/download/) — a
   process lane with a missing binary reports itself down with a plain "needs ..." sentence,
   it does not crash. Then `config.json`'s `"lanes"` needs a **process** lane, not a ComfyUI
   one — copy-paste this as the whole file (adjust `"port"`/`"bind"` if you changed them):

   ```json
   {
     "port": 3998,
     "bind": "127.0.0.1",
     "lanes": [
       {"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]}
     ]
   }
   ```

   A process lane needs no `host`/`port` of its own (it runs a local program, not a ComfyUI
   instance) but **must** declare a non-empty `caps` — nothing about it is discoverable until
   its binaries are.
3. **No ComfyUI yet, but they have a GPU** → ComfyUI itself must be installed first. Do not
   invent install steps for it here — point the user at ComfyUI's own installation docs
   (https://github.com/comfyanonymous/ComfyUI) and come back to this file once `python
   main.py` (or their ComfyUI's own start command) answers on some `host:port`.

## Install + start

Setting up for a user, do all of this by default. It is the path that makes every feature work:

```
test -e config.json || cp config.example.json config.json   # never overwrite a real config
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py
```

`requirements.txt` (numpy, Pillow) is small, and although the server runs without it, a user
setup should always install it. Without Pillow the recipe-removed **picture** download is
refused (422) and Pixel Art is unavailable. Use the venv, never a global `pip install`:
modern distro Python refuses a bare `pip install` (PEP 668 "externally managed
environment"; confirmed on a stock Ubuntu 26.04/Python 3.14 box, exit 1).

**Once `.venv/` exists, use `.venv/bin/python` for everything from then on — not just
starting the server.** That includes any one-off script YOU (the agent) write to check,
poll, or verify something, not only the app itself: `python3` (system Python) does not see
`requirements.txt`'s packages, so a helper script run with plain `python3` after this point
hits `ModuleNotFoundError: PIL` (or numpy) even though the install above succeeded.

- The server starts and answers every request without `requirements.txt` installed at all.
  What degrades without it: the recipe-removed picture download (refused with a plain
  sentence), Pixel Art's colour-lock/dither step, and the picture→video image-fit step. Each
  says what to install instead of crashing.
- `ffmpeg` on `PATH` is likewise optional: without it the Cutting Room still opens, it just
  cannot build a cut yet.
- `config.json` is **required** and its `"lanes"` list must not be empty — the app refuses
  to start without at least one lane (see "Troubleshooting"). It is gitignored: never commit
  or share it (see "Rules for the agent"). `test -e config.json || cp ...` above only creates
  it if it doesn't already exist — a bare `cp` would silently clobber a real one on a re-run.
- The example config binds `"bind": "127.0.0.1"` — localhost only. `python3 server.py` (or
  `.venv/bin/python server.py`) runs in the **foreground** — it does not return a prompt.
  Leave it running in one terminal (or background it: `... server.py &`) and run every
  "Verify" command below from a second terminal or shell. Success looks like this on stdout:

  ```
  Black Wire Forge is up on http://127.0.0.1:3998
  Loaded 1 lane(s) from config.json
  Config file: /path/to/config.json
  ```

  (`server.py`'s `main()`.)

## Verify

Run these against the running server (defaults to port 3998; use whatever `"port"` is in
`config.json`):

```
curl -s http://127.0.0.1:3998/api/health
```
Expect `{"ok": true, "port": 3998, "lanes": N}` where `N` is the lane count. This only proves
the app itself is up, not any lane.

```
curl -s http://127.0.0.1:3998/api/lanes
```
Returns `{"lanes": [...], "title": ..., "fleet_llm": ...}`. For each lane object:

- `"up"` (bool) — whether the app reached that lane's ComfyUI `/system_stats` on its most
  recent poll, not necessarily just now; `"checked"` carries that poll's timestamp. `false`
  means it could not; check `"err"` (string), which carries the raw connection error (e.g. a
  connection-refused message) when `up` is `false`.
- `"caps"` — the capabilities (`"image"`, `"video"`, `"audio"`, `"3d"`) this lane currently
  offers, after discovery has weighed in; `"declared_caps"` is what `config.json` asked for.
- `"able"` — an object keyed by **mode/ability name** (e.g. `"t2i"`, `"song"`, `"fl2va"`), not
  by capability: whether the lane's discovered model files satisfy that specific mode right
  now. A pack's `cap_from` abilities are OR'd onto the cap name too (so `able["image"]` also
  appears, true if any of that cap's abilities is), but the per-mode keys are what most packs
  actually populate — read `engines.abilities()`'s two-pass computation in `engines/__init__.py`
  before assuming `able` is capability-shaped.
- `"missing_image"`, `"missing_video"`, `"missing_audio"`, `"missing_3d"` — plain-English
  sentences (from each engine pack's own `words`) naming which model files are still needed
  for that capability, populated whenever the lane declares that cap but `able` says no --
  see `lanes_payload()` in `server.py`: this key is computed straight from `able`/`models_for()`
  on every call, with no dependency on `"discovered"`.
- `"discovered"` (bool) — whether discovery has run against this lane at least once. Only
  `"caps"` waits on it (mirroring `declared_caps` until then, per `lanes_payload()`'s
  `effective` line); `"missing_*"` does NOT wait — before discovery has run, `models_for()`
  reflects only config overrides (usually none), so `able` is false for every declared cap and
  `missing_*` already lists the full requirement, with `"discovered": false` alongside it.

```
curl -s "http://127.0.0.1:3998/api/engines?lane=<lane-id>"
```
(`<lane-id>` is a lane's `"id"` from `/api/lanes`.) Returns one key per capability
(`{"modes": [...], "cap_word": ..., "cap_order": ...}`), plus two more top-level keys that are
NOT capabilities — `"rooms"` (task-room groupings) and `"helper"` (bool, whether an
OpenAI-compatible prompt helper is configured) — skip both when iterating capability keys.
Each mode object has:

- `"available"` (bool) and `"missing"` (list of strings) — whether that lane's **discovered
  model files** (and any pack-level local-package dependency) satisfy this mode. This does
  **not** by itself mean the mode can run right now: it says nothing about whether the lane is
  currently reachable (`/api/lanes`' `"up"`), whether the lane's declared `"caps"` include this
  mode's capability, or whether the lane's `"kind"` matches `"lane_kind"` below — check all
  three from `/api/lanes` as well before treating a mode as truly ready. When `"lane_kind"`
  does NOT match the lane you asked about, `"missing"` says so in one plain sentence (e.g. "this
  mode runs on a process lane, not a ComfyUI lane: add ... to config.json") instead of listing
  model files — it is not actionable to install anything in that case.
- `"fields"` — the exact form fields that mode's graph builder expects (id/label/type/etc.).
- `"lane_kind"` — `"comfy"` or `"process"`; which kind of lane the mode needs.

Without `?lane=`, every mode reports `"available": false` (there is nothing to check
availability against) — always pass a real lane id.

- `curl -s "http://127.0.0.1:3998/api/guide?room=cutting"` — the room's guide (`"guide"`,
  `null` for a room without one), whether a helper is configured (`"helper"`), and its reported
  context (`"helper_context"`, `null` when unknown).
- `POST /api/guide/chat` with `{"room", "verbosity": "compact"|"verbose", "messages": [...]}` —
  one guide turn; `409` with `"no_brain": true` when no `"helper"` is configured. An optional
  `"context": {"mode": <a mode of that room>, "fields": {<field id>: <value>}}` tells the guide
  the room's current mode and field values (`400` with a sentence if it names a mode or field
  the room does not have).

## Make something and get the file

The exact request/response shapes below come from reading `generate()`, `jobs_payload()`,
and `proxy_view()` in `server.py` directly — trust these over guessing a field name.

**1. Submit the job** — `POST /api/generate`, JSON body, at minimum `lane`, `kind`, `mode`,
and `prompt` (everything else has a default):

```
curl -s -X POST http://127.0.0.1:3998/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"lane": "local", "kind": "image", "mode": "t2i", "prompt": "a lighthouse at sunset"}'
```

`lane` is a lane's `"id"` from `/api/lanes`; `kind` is a capability (`"image"`, `"video"`,
`"audio"`, `"3d"`); `mode` is one of that capability's mode ids from `/api/engines?lane=...`
(`"t2i"` for a plain picture, `"edit"` for an edit). Any other field a mode's own `"fields"`
declares (from `/api/engines`) goes either as a top-level key, or nested under a `"values"`
object — the request-value lookup checks both, top-level first. Success:
`{"ok": true, "job": {"id": "<job-id>", "status": "queued", ...}, "notes": [...]}`. Refusal
(bad lane, mode unavailable, a field that fails validation, ...):
`{"ok": false, "error": "<plain sentence>"}`, HTTP 400/409. Keep the `"id"` — everything
below is keyed on it.

**2. Poll for it to finish** — `GET /api/jobs`:

```
curl -s "http://127.0.0.1:3998/api/jobs?limit=5"
```

Returns `{"jobs": [...], "log": [...], "now": <epoch>}`. Find your job by its `"id"` in
`"jobs"` and watch `"status"`: `"queued"` → `"running"` → `"done"` / `"error"` /
`"interrupted"`. Only `"done"` means a finished render; on `"done"`, `"outputs"` is a
non-empty list of `{"filename": ..., "subfolder": ..., "type": ..., ...}` — one entry per
file the job produced (a `post`-step pack, e.g. Pixel Art, appends a SECOND entry with
`"type": "local"`, alongside the lane's own; most modes produce exactly one). On `"error"`,
`"error"` carries the plain, already-user-facing sentence — show it verbatim, do not
paraphrase it.

**3. Get the actual file** — `GET /api/view`, using the `filename`/`subfolder`/`type` from
the job's `outputs` entry you picked, plus the same `lane` id:

```
curl -s -o lighthouse.png \
  "http://127.0.0.1:3998/api/view?lane=local&filename=<filename>&subfolder=<subfolder>&type=output&dl=1"
```

- `dl=1` — the recipe-removed copy: `strip_metadata()` strips the embedded workflow/prompt
  text chunk (model filenames, your full prompt) before the bytes leave the app. This is the
  one to hand to a user who might share the file.
- `keep_recipe=1` in place of `dl=1` — the original file, metadata and all.
- If cleaning genuinely fails for a format the app claims to support (unsupported
  sub-format, a missing dependency, a parse failure), `dl=1` is REFUSED rather than silently
  serving the original: HTTP 422, `{"error": "Could not remove the recipe from this file, so
  it was not downloaded. Use the plain download if you want the original."}`. Retry with
  `keep_recipe=1` if the user is fine with the unstripped original.
- `type=local` (an entry from a `post` step, see above) is served from the app's own disk,
  never proxied to a lane — the same `lane`/`dl`/`keep_recipe` query shape still applies.

**Where the file actually lives — answer with BOTH of these, not just one:**

1. **On the lane's own ComfyUI machine**, in its ComfyUI output folder — this is the render
   itself, for every `"type": "output"` entry, on EVERY job regardless of mode. The app never
   moves it; `/api/view` only proxies a copy over HTTP on request.
2. **A local copy under `data/outputs/<job id>/`, on the machine running this app — but only
   when one exists.** It exists in exactly two cases (`server.py`'s `run_process_job()`, the
   `job_dir = os.path.join(LOCAL_OUTPUTS_DIR, jid)` line, and `run_post_step()`): (a) a
   **process**-lane job (e.g. the turntable pack) — the whole render happens locally, so this
   IS its only copy; (b) a mode with its own `post` step (e.g. Pixel Art's quantise) — the
   transformed file lands here as an ADDITIONAL output (`"type": "local"`) alongside the
   lane's own render, never replacing it. A plain ComfyUI-lane picture (Qwen `t2i`, no `post`
   step) has NO local copy at all — check `job["outputs"]` for a `"type": "local"` entry
   before claiming one exists.

Only save a copy into a path the user actually asked for (their Downloads folder, a project
directory, ...) — don't assume where they want it written.

## Fixing what's missing

Every engine pack under `engines/` declares its own model **roles** and the ComfyUI
**node classes** its graph needs. Read the pack itself for the authoritative list (`roles`
+ `graphs` in each pack's `ENGINE` dict) — this table is a snapshot:

| Pack (`engines/`) | Cap | Model roles needed (plain words) | Node classes used in its graph(s) |
|---|---|---|---|
| `engines/qwen_image.py` | image | the Qwen-Image model, its text encoder, its image decoder | `UNETLoader` **or** `UnetLoaderGGUF` (conditional on the model file's extension, via `engines/__init__.py`'s `unet_loader()`), `CLIPLoader`, `VAELoader`, `TextEncodeQwenImage21`, `APG`, `FreSca`, `ModelSamplingAuraFlow`, `KSampler`, `VAEDecode`, `SaveImage`, `LoadImage`, `EmptyLatentImage` |
| `engines/ltx.py` | video | the LTX video model, the LTX text encoder, the LTX video decoder, the LTX audio decoder, the LTX upscaler | `UnetLoaderGGUF`, `CLIPLoader`, `CLIPTextEncode`, `VAELoader`, `EmptyLTXVLatentVideo`, `LatentUpscaleModelLoader`, `LTXVConditioning`, `LTXVAddGuide`, `LTXVCropGuides`, `LTXVImgToVideoInplace`, `LTXVPreprocess`, `LTXVContextWindows`, `LTXVLoopingSampler`, `LTXVLatentUpsampler`, `LTXVDualCFGGuider`, `LTXVConcatAVLatent`, `LTXVSeparateAVLatent`, `LTXVAudioVAEDecode`, `LTXVEmptyLatentAudio`, `KSamplerSelect`, `RandomNoise`, `ManualSigmas`, `SamplerCustomAdvanced`, `CFGGuider`, `ImageScale`, `LoadImage`, `CreateVideo`, `SaveVideo`, `VAEDecodeTiled` |
| `engines/minimax_h3.py` | video | the H3 video model, the H3 reference model, the H3 text encoder (nvfp4 or int8), the H3 video decoder, the H3 audio decoder, **optionally** the speed pack (LoRA, for the Fast/Fast+/Fast best quality tiers only -- neither `fl2va` nor `ref2v` requires it) | `UNETLoader` **or** `UnetLoaderGGUF` (conditional, via `unet_loader()`), `LoraLoaderModelOnly`, `CLIPLoader`, `VAELoader`, `MiniMaxH3ImageToVideo`, `MiniMaxH3ReferenceToVideo`, `MiniMaxH3MotionContext`, `MiniMaxH3MotionContextTrim`, `KSamplerSelect`, `RandomNoise`, `BasicScheduler`, `BasicGuider`, `SamplerCustomAdvanced`, `VAEDecode`, `VAEDecodeAudio`, `LoadImage`, `LoadVideo`, `GetVideoComponents`, `CreateVideo`, `SaveVideo` |
| `engines/audio.py` | audio | per mode: the ACE-Step song model (+ two text encoders + decoder), the MiniMax-Music3 model (+ encoder/decoder), the Stable Audio Open model (+ encoder), the YuE2-3B model, the SheetSage2 audio encoder | `UNETLoader`, `DualCLIPLoader` (ACE-Step's two text encoders, loaded together), `CLIPLoader`, `CLIPTextEncode`, `TextEncodeAceStepAudio1.5`, `EmptyAceStep1.5LatentAudio`, `AudioEncoderLoader`, `CheckpointLoaderSimple`, `ModelSamplingAuraFlow`, `KSampler`, `VAELoader`, `VAEDecodeAudio`, `EmptyLatentAudio`, `EmptyMiniMaxMusic3LatentAudio`, `EmptyYuE2LatentAudio`, `MiniMaxMusic3TextEncode`, `YuE2GenerateMusic`, `YuE2GenerateABC`, `SheetSage2AudioToABC`, `ConditioningZeroOut`, `MiniMaxParametricEQ`, `MiniMaxAutoEQAnalyze`, `MiniMaxMasteringCompressor`, `LoadAudio`, `SaveAudioAdvanced`, `PreviewAny` |
| `engines/cleanup.py` | image | the background removal model, the upscaling model | `LoadBackgroundRemovalModel`, `RemoveBackground`, `UpscaleModelLoader`, `ImageUpscaleWithModel`, `InvertMask`, `JoinImageWithAlpha`, `LoadImage`, `SaveImage` |
| `engines/pixelart.py` | image | the picture model (Qwen-Image), its text encoder, its image decoder, the background removal model | reuses `engines/qwen_image.py`'s graph plus `LoadBackgroundRemovalModel`, `RemoveBackground`, `InvertMask`, `JoinImageWithAlpha`, `SaveImage` |
| `engines/mesh3d.py` | 3d | the TRELLIS2 model, its shape decoder, its texture decoder, its image encoder, the background removal model | `UNETLoader`, `VAELoader`, `CLIPVisionLoader`, `Trellis2Conditioning`, `Trellis2ShapeStage`, `Trellis2TextureStage`, `EmptyTrellis2LatentStructure`, `VaeDecodeShapeTrellis`, `VaeDecodeStructureTrellis2`, `VaeDecodeTextureTrellis`, `BakeTextureFromVoxel`, `ApplyTextureToMesh`, `MeshSmoothNormals`, `UnwrapMesh`, `GetMeshInfo`, `MeshToFile3D`, `Save3DAdvanced`, `KSampler`, `ModelSamplingSD3`, `RescaleCFG`, `CFGOverride`, `LoadBackgroundRemovalModel`, `RemoveBackground`, `ImageCropToMask`, `LoadImage` |
| `engines/turntable.py` | 3d | **not ComfyUI nodes** — a `"process"` lane needing the `blender` and `ffmpeg` binaries on `PATH` (its own `"bins"` dict) | n/a (runs Blender via a CLI script + an `ffmpeg` encode step, `runner.py`) |

Model **filenames** discovery looks for are governed by each pack's `roles` dict (name
fragments, e.g. `engines/qwen_image.py`'s UNet role wants a filename containing `qwen_image`, and
prefers one also containing `2.1`) — do not guess a filename requirement not in that dict.

## Hardware

The repo does not carry a general VRAM-by-mode table — most engine packs record render
*time*, not VRAM, or nothing at all. The one measured VRAM figure in the code:

- **YuE2-3B (audio.py, `cover`/`yue2` modes)**: the BF16 checkpoint peaks at **~9.86 GB VRAM**
  on an RTX 5080 (16 GB card) — chosen over the int8 build (~7.73 GB peak) because int8 was
  measured ~3.7x slower with no native int8 matmul path (`engines/audio.py`, function
  docstring near the `yue2`/cover graph builder).

For every other mode/model: **not measured — check the model's own page** (Hugging Face
model card, or the tool's own docs) before assuming it fits your GPU.

## Rules for the agent

- Keep `"bind": "127.0.0.1"` unless the user explicitly asks for LAN access **and**
  understands the app has no login (`SECURITY.md`: anyone who can reach the port can see
  every job and start new ones). If they ask for LAN, add their reverse-proxy/mDNS hostname
  to `"allowed_hosts"` rather than binding wider than needed where possible.
- Never commit or share `config.json` — it carries the user's real machine addresses and is
  gitignored on purpose.
- Before downloading any model weights, tell the user the file size and licence and get an
  explicit yes. Call out non-commercial licences (Qwen-Image 2.1, YuE2-3B) and
  territory-restricted ones (MiniMax-H3 — open outside the EU/UK/South Korea/USA) by name —
  see "Getting the models" below (or `docs/MODELS.md` if that section was moved out) for the
  full per-model source, size, and licence list before recommending a download.
- Don't disable or work around the request guard (`request_refusal()` in `server.py`) — it
  is the only thing standing between this app and a browser-based attack against a
  no-login server. If it refuses something legitimate, fix `"allowed_hosts"`, don't patch
  the guard out.

## Tests

**Not a setup step.** Setting this app up for a user, or checking that ComfyUI/a mode/a lane
works, needs none of this — use "Verify" and "Make something and get the file" above instead.
Run the suite only when the user asks for it, or after YOU change code in this repo.

```
scripts/run-tests.sh
```

Runs `tests/test_*.py` one suite at a time, each under GNU `timeout` (falling back to
`gtimeout`, then to no time limit at all with one printed note, if neither is on `PATH` --
stock macOS has neither). The full suite additionally needs `requirements-dev.txt`
(Playwright) plus `python3 -m playwright install --with-deps chromium`, and `ffmpeg`/`ffprobe`
on `PATH`. Without any of these, the suites that need them print a plain `SKIP` reason and
exit cleanly — a clean clone with none of the optional deps still passes every suite that
doesn't need them. A suite exiting non-zero for any other reason is a real `FAIL`. No
`config.json` is required to run the suite.

## Troubleshooting

| Symptom / sentence (grep `server.py` for the exact wording) | Cause | Fix |
|---|---|---|
| `No config file at <path>...` (on startup) | `config.json` doesn't exist yet | `cp config.example.json config.json`, then edit it |
| `<config> needs a non-empty "lanes" list -- one entry per ComfyUI instance.` | `"lanes"` is missing or `[]` | add at least one lane object |
| `<config> is not valid JSON: ...` | malformed JSON (commented-out lines, trailing comma) | fix the JSON; it allows neither |
| `Port <N> is already in use.` | another process (maybe a previous run) already has the port | `lsof -nP -iTCP:<N> -sTCP:LISTEN`, or change `"port"` in `config.json` |
| A lane's `"up"` is `false` in `/api/lanes`, `"err"` non-empty | that lane's ComfyUI isn't reachable at its configured `host`/`port` | start ComfyUI there, or fix the lane's `host`/`port` |
| A mode's `"missing"` list is non-empty in `/api/engines` | the lane lacks a model file (or local package) a pack's role needs | see "Fixing what's missing" above; install/point discovery at the right file |
| `Requests to this app must carry a Host header.` / `That Host header is not a valid address.` / `This app only answers to its own address, not ...` (403/400) | request guard rejected the `Host` header (DNS-rebinding protection) | reach the app by `localhost`/its own IP/hostname, or add the name to `"allowed_hosts"` |
| `Send JSON (Content-Type: application/json).` / `Send the file as a multipart/form-data upload.` (415) | a `POST` had the wrong `Content-Type` | send the header the endpoint expects (JSON everywhere except `/api/upload`, which wants multipart) |
| `Requests from another site are refused.` (403) | cross-site `Origin`/`Sec-Fetch-Site` on a `POST` | only call the API from a page served by this same app |
| A "clean download" is refused rather than served | the file's format isn't one `sanitize.py` can actually strip (e.g. WAV/M4A) or a required dependency (Pillow/ffmpeg) is missing | check `/api/credits`' `clean_download`/`clean_audio_exts` for what's currently cleanable, or use "Keep the recipe" instead |

## Getting the models

Black Wire Forge ships no weights of its own — see "What this is / is not" above. For every
built-in pack/mode, `docs/MODELS.md` gives, per model role: the exact Hugging Face repo id and
filename (or the official page, for the one tool not on HF), its size and licence (with any
non-commercial or territory gate restated), the ComfyUI `models/<subdir>` the loader node reads
from, and a PASS/FAIL check of that upstream filename against the pack's own discovery rule
(`engines/__init__.py`'s `role_rules()` + `server.py`'s `pick_model()`) — several upstream names
do NOT match as downloaded and must be renamed, or placed in a specific subfolder, before
discovery will see them; `docs/MODELS.md` says exactly what to rename or where to put it. It
also names the custom-node package (with its repo URL) behind every non-core node class a pack
uses, and says which classes ship in core ComfyUI already. Tell the user the file size and
licence and get an explicit yes before recommending any download (see "Rules for the agent").

## Where to read further

- `README.md` — full install/config reference
- `docs/MODELS.md` — where to get every built-in model, its licence, and its discovery-match check
- `docs/ARCHITECTURE.md` — lanes, discovery, packs, rooms, jobs, sequences, the cut
- `docs/WRITING-A-PACK.md` — the engine-pack contract, for adding a new model family
- `SECURITY.md` — exactly what the request guard does and does not protect against
