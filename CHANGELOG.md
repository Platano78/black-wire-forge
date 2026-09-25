# Changelog

All notable changes to Black Wire Forge are recorded here.

## v1.1.0 — 2026-09-25

### Added

- Every room now has a guide you can talk to. The guide page holds a conversation with it
  through the configured helper, keeps history on the page, and offers a Compact/Verbose
  toggle. "Help me write this" starts that same conversation with a mode-specific focus,
  asks what matters for that mode, fills the fields from your answers, and shows a preview
  before using them.
- The guide has sourced knowledge for every room group — Sound, Picture, Motion, Object
  and Film — so its advice is specific to what the room does.
- The helper can now see what you've already set: its messages carry your current field
  values so it doesn't repeat itself or guess wrong.
- Mode writers turn a topic into the right fields for that mode. The first one is Music's
  song writer — name a voice and it writes lyrics sized to the duration, so a song with
  words never quietly renders as an instrumental.
- Sound modes (Background music, Cover, Sound FX, planned song) each write their own
  fields from a topic. Motion modes (Video, Long take, Talking Head) do the
  same, and Talking Head now computes the correct clip length from the spoken line
  instead of trusting the helper to count words.
- Picture and 3D writers build prompts from subjects, style, setting and action, and 3D
  "Help me write this" can write a source picture for the Picture room.
- Pixel Art has its own writer that asks for a sprite (flat colours, thick outline, clear
  view) — the old advice that produced photoreal close-ups is gone.
- A finished picture can get a "Not right? Tell the guide" fix. Send the picture, tell
  the guide what's wrong, and it returns a revised prompt or an edit instruction.
- In the Cutting Room, "Write this shot" runs that shot's own engine writer so the
  prompt matches the engine (LTX, H3, etc.). The 3D guide can compare two
  pictures of an object and say when they don't match.
- Modes without a dedicated writer get a generic one built from the room guide and the
  mode's prompt guide, with all values checked against the mode's fields.

### Changed

- The guide panel's "What the brain was asked" disclosure is gone; the preview of the
  fields stays.
- The three columns now share width in proportion so the history and the form get real
  room on wide screens, while small screens keep the sides usable.
- Pixel Art shows the post-step sprite (scaled with square pixels) as the result; the raw
  render is one click away as "Before the pixel step".
- A song with words but no voice now asks before rendering instead of producing a silent
  instrumental.

### Fixed

- Sending a lane as a list or object now gives a plain error instead of a 500 crash.
- Pixel Art's palette step works on Pillow older than 9.1.
- The H3 check no longer flags the vendor's own task-prefix bracket as a problem; other
  brackets are still caught.
- A helper that thinks before it answers (a reasoning model) no longer fails every writer
  with "didn't come back in the expected shape" or leaves an empty guide bubble when it
  spends its whole budget thinking: the app asks once more with four times the room, then
  says plainly what to change. The new `helper.max_tokens` config key gives every guide
  reply at least that many tokens.
- The "Not right? Tell the guide" fixers no longer open with "I can't see the picture" when
  the helper can see it: the text-only rules are sent only to a helper that cannot see.
- Help has a section on the room's guide: writing with it, the preview, Compact/Verbose,
  "Describe this picture", "Not right?", "Edit this result", and what shows with no helper.
- A process lane's list of what is missing names the programs it needs (Blender, ffmpeg),
  never GPU model files.
- Downloading from a lane that is not answering, or that no longer has the file, gives a
  plain sentence instead of a 500.
- A request whose Host header carries the wrong port is refused with how to fix it (a proxy
  must pass the app's own port, or none), not advice to edit `allowed_hosts`, which cannot
  help.
- The Object guide no longer says more frames keep a turntable the same length: the video
  lasts frames ÷ fps.
- `config.example.json`'s lane is `local` ("This machine"), so the README and AGENTS.md
  examples run as written. AGENTS.md's no-GPU path now walks a turntable render from upload
  to download, and no longer tells you to overwrite an existing config.
- The test suites that need Pillow print a plain SKIP without it instead of crashing, and
  `scripts/run-tests.sh` uses the repo's `.venv` when there is one.
- The guides no longer cite internal documents, and several docs were corrected against the
  code: startup lines, the 503 while a lane's models are still being read, `/api/guide`'s
  fields, how a sequence take is chosen, where cuts are written, the sample pack in
  WRITING-A-PACK.md, and the Real-ESRGAN licence source.

## v1.0.2 — 2026-09-24

### Fixed

- The UI smoke test's Cutting Room banner check expected v1.0.0's wording, so on v1.0.1 the full
  suite reported 1 failure. The app was unaffected; the test now checks the current banner.

## v1.0.1 — 2026-09-24

### Fixed

- A mode's main file input is visible on arrival. Talking Head's face picture sat inside the
  collapsed Recipe drawer whenever the mode also had a prompt box, while the page said "Needs a
  picture first" and pointed at another room. Primary file inputs now take the slot above the
  prompt; other file inputs stay in the drawer.
- "Needs a picture first" now says you can add your own, before offering to make one.
- The Cutting Room no longer says "the cut itself arrives later". The cut has shipped since v1.0.0.
- The UI smoke test checks that every primary file input is visible, not just present. It
  counted the hidden input as rendered and passed.

## v1.0.0 — 2026-09-24

First public release.

### What it does

A hand-operated web UI in front of one or more ComfyUI instances ("lanes") on your own
machines. Pick a room, write a prompt or add files, press Make — the app picks a graph,
sends it to a lane that can run it, and shows you the result. Navigation and generation
controls use task-oriented labels; machine status and the Credits panel still identify
the installed engines and their licences.

- **Rooms by task** — Music, Cover, Sound FX, Picture, Pixel Art, Clean-up, Textures,
  Video, Talking Head, 3D, and the Cutting Room. All built-in rooms stay visible;
  unavailable ones are dimmed. Within each room, availability reflects the lane's
  configured capabilities, the models discovery actually found, and local dependency
  checks (Pillow, `ffmpeg`, and so on).
- **Live lane discovery** — the app polls each configured lane, finds which model files
  it has, and works out which modes it can run from that alone; a lane with nothing
  installed says so plainly instead of just failing later.
- **Guided generation** — every mode declares its own form fields (with units, valid
  ranges and slider ranges), named presets with a citation for where the setting was
  measured, quality tiers ("quick" vs. "high end") instead of raw step counts, and a
  one-click "Try this" example per mode.
- **Optional prompt helper** — "Help me write this" / "Describe this picture", against
  any OpenAI-compatible chat endpoint you configure; omit the config and the buttons
  disappear.
- **Process-lane engines** — some jobs run as a local program instead of a ComfyUI graph;
  today that's the 3D turntable (Blender + Cycles, CPU-rendered).
- **The Cutting Room** — build a sequence out of picture, video and sound slots; each
  slot can hold several takes with one picked, take reference images, and "cable" one
  slot's output into another's input field (used to carry a shot's motion and sound into
  the next one). Import a script and its lines become pre-filled slots. When you're
  ready, cut: the app builds one rendered edit from your picked takes, with per-clip
  titles, one consistent loudness normalization across the whole joined track (measured in
  two ffmpeg passes, never per-clip), and consistent output sizing —
  refusing synchronously, and naming the shot, if something isn't ready to cut yet.
- **Sanitized sharing** — "Download (recipe removed)" strips the embedded prompt and
  model filenames out of a picture, audio or video file before it leaves the app, and is
  refused rather than silently served untouched when the strip can't actually happen
  (an unsupported sub-format or a missing dependency); "Keep the recipe" downloads it
  untouched on purpose. See `SECURITY.md`.
- **Licence stamps** — every finished job and every installed engine carries its own
  licence, visible on the job and in a Credits panel.
- **No login, LAN-trusted** — the app protects itself against DNS rebinding and
  cross-site requests (see `SECURITY.md`) but has no accounts; it's meant to run on a
  trusted home network, not the open internet.

### Under the hood

- Engine packs (`engines/`) are self-contained and auto-discovered; the core app knows
  no model names (`docs/ARCHITECTURE.md`, `docs/WRITING-A-PACK.md`).
- Runs on the Python standard library alone; `numpy`/Pillow are optional and only needed
  for Pixel Art's post-step and the picture-to-video image-fit step — both degrade to a
  plain "install this to enable" message without them.
