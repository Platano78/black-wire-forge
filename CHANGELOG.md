# Changelog

All notable changes to Black Wire Forge are recorded here.

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
