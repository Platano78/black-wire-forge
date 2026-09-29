# Changelog

All notable changes to Black Wire Forge are recorded here.

## Unreleased

### Added

- A **Characters** room: one picture of a character and a name become one design sheet (a title
  column, a large hero pose, front/side/back views, three action angles, three silhouettes, three
  expressions and a grid of close-up details) on the Qwen-Image 2.1 edit encoder. The Characters
  guide reads the picture and the name and writes the ten-section sheet prompt into the Sheet
  prompt box for you to read and edit before Make. Sheet size is Quick (1 MP), Balanced (3.4 MP,
  the default) or Large (6 MP). It needs no new model files; two speed-only nodes
  (`ModelAttentionBackend`, `QwenImage21Cache`) are used when the lane has them and skipped when
  it does not. A pack can now declare `optional_nodes` and `generic_modes`, and a writer a
  `topic_default`, so a writer needs no words of its own once its picture is attached.

- UX flow pass (owner rulings, 2026-09-28): the picked engine is now a chip beside the room
  heading ("Music · Background music ▾"), always visible (no caret/popover for a single-engine
  room); Style and Lyrics (or any pack's own Content-group pair) sit directly under the prompt
  box, before Quality/Sound; a sound room's empty state explains what Style vs Lyrics are;
  time estimates show a range from the recent jobs ("1–3 min · the first run after a restart is
  slower") instead of one point figure; a same-tab double press of Make shows an inline "That's
  the same as the one still rendering — make another anyway?" notice instead of silently queuing
  a duplicate; a running job's History row and Monitor show a percentage and stage ("Rendering ·
  stage 1 of 2 · 45%") plus elapsed time, computed from the graph's own sampling-stage nodes, not
  a raw step counter; clearing a guide conversation is now recorded on the server (a generation
  counter), so a device with a longer, stale local copy adopts the clear instead of silently
  reviving it; the style browser badges a workflow-only pack (IC-LoRA/upscaler/speed, matched by
  the family's own exclusion words) as "Needs its own workflow — won't work as a style", and a
  catalog entry already on the lane's disk shows "Installed" instead of Get it. Remove can now
  also delete the lane's own output file, opt-in per lane (`"outputs": {"dir": "..."}` in
  config.json, same shape as LoRA downloads) and off by default.

### Fixed

- UX-2 #11's "Installed" badge never showed: ComfyUI's lora pool lists names WITH their
  subfolder (`minimax_h3/x.safetensors`, possibly backslash-separated on Windows hosts), but
  catalog filenames are bare, so nothing ever matched -- now compared by basename, split on
  both `/` and `\`.

- Field GROUPS (Content/Sound/Quality/...) were sorted by their per-group `order` number
  globally across the whole form, so a later group whose first field happened to carry a lower
  `order` (e.g. Sound's bpm:1) rendered before an earlier-declared group (Content's lyrics:2) --
  the underlying mechanism behind "the YuE2 job ran with no lyrics".

- Style packs (LoRAs) for the Picture room (t2i, edit) and Pixel Art: pick up to two style LoRAs
  already installed on the lane, each with its own strength (0-1.5), chained after the model
  loader; picking none leaves the render graph byte-for-byte the same as before. A "Browse
  styles" drawer lists matching LoRAs from the Hugging Face Hub's public API, one card per repo
  with a readable name/author, its model card's own first-sentence description (falling back to
  "No description on the model card."), trigger words and a recommended strength when the card
  states them, a preview image when the card's front matter has one on huggingface.co, licence
  ("licence unknown" when the card states none) and file size; every pack is listed, an
  NSFW-tagged one carrying a plain "NSFW" badge rather than being hidden. On a lane whose room
  offers more than one style family (e.g. a video room's two engines), the drawer shows a tab
  per family and downloads land under that family's own subfolder
  (`<loras_dir>/<family>/<file>`), so its filename doesn't have to name the model for the lane
  to find it. Downloading a file is opt-in per lane (`"downloads": {"loras_dir": "..."}` in
  config.json) and off by default; without it, "Get it" prints the exact
  `hf download ... --local-dir <ComfyUI>/models/loras/<folder>` command to run on the ComfyUI
  machine instead. A download is validated server-side end to end (only a Hub URL built from a
  catalog id, a bare `.safetensors` filename and a declared family folder that cannot leave the
  lane's LoRA folder, a size cap, one at a time per lane, cancellable) before any byte reaches
  disk.

- "Write this shot" and "Help me write this" now see a shot's own starting or face picture --
  the LTX shot writer (its starting picture), the H3 fl2va shot writer (its starting picture),
  and the Talking Head line writer (its face picture) -- the same way the Picture room's edit
  writer already sees its own pictures. A vision-capable helper matches the picture's light
  instead of guessing from earlier shots' words alone; with no vision-capable helper, behaviour
  is unchanged (the existing "this helper can't see pictures" note). H3's ref2v and continue
  writers are unchanged: ref2v's own prompt is written around not seeing its references, and
  continue has no picture field at all (it carries the previous shot over a video jack).
- The guide conversation (including guide actions and their done markers) is now also saved on
  the server, per room or per open sequence, so it survives a reload on another device, a
  blocked-storage browser, or clearing site data. The browser's own copy is still the fast,
  immediate cache; the server copy is a best-effort background sync, last write wins.
- Style packs (LoRAs) extended to every video and music engine: LTX-2.5 (all three modes,
  chained once and shared across two-stage/windowed sampling), MiniMax-H3 (fl2va, ref2v,
  continue -- chained after the turbo speed LoRA when one is on), ACE-Step 1.5 and MiniMax-Music3
  song/music modes, and YuE2 (yue2, cover). Same up-to-two-styles/strength shape as the Picture
  room; picking none leaves every graph byte-for-byte the same as before. The "Browse styles"
  catalog contract (`engines.style_catalogs()`) now carries a family per engine (id, label, cap,
  modes, the pool-match rule, and the Hugging Face base-model id) instead of one Qwen-only entry.
- MiniMax-Music3 LoRAs downloaded through "Browse styles" are now converted automatically after
  the download, before they're ever listed: every Music3 LoRA on the Hub is trained against
  separate q/k/v attention projections, but the engine's attention is one fused matrix, so a
  file downloaded as-is silently applied only a quarter of its intended effect (its `to_out`
  quarter). The conversion is an exact merge (no approximation, no new heavy dependency), and a
  file it can't convert is refused with a plain sentence rather than guessed at; a conversion
  failure fails the whole download, leaving nothing partial behind. A lane with downloads off
  still shows the copy-paste `hf download` command, now with a second line when the pack needs
  converting. "Browse styles" also refuses a family that's declared somewhere in the catalog but
  not actually present on the lane you're downloading to, and reads a few more forms of trigger
  word from a model card ("Trigger Prompt", a bare "Trigger:" label, "activation token(s)", and
  the README front matter's own `instance_prompt:` field).

### Fixed

- The Talking Head Line writer no longer overwrites a Length you set yourself in the form. It
  used to derive Length from the written line's word count on every write, even when the current
  form already carried a Length you had changed by hand. It now leaves Length alone once it
  differs from the field's own default (97 frames), and still derives it from the line when
  Length is at that default or the form's current values aren't available to it.
- The Picture guide (and every room guide) no longer offers "Help me write this" in a room with
  no available engine. The offer now checks the room has at least one installed, reachable mode
  before showing; a room with none keeps showing its existing "nothing installed" / "no machine
  reachable" note instead.

- Video (LTX) and YuE2 no longer depend on one particular build of their model. LTX used to
  find its video model only if the filename said `gguf`, and always loaded it with the GGUF
  loader; YuE2 refused the `int8` checkpoint. Both now use whichever build you have: LTX picks
  `UNETLoader` or `UnetLoaderGGUF` from the file type, and YuE2 takes the bf16 or int8
  checkpoint. A lane holding only a native LTX build, or only the int8 YuE2 file, now shows
  those modes as available instead of missing.
- Music (MiniMax-Music3) songs end when the words end. The model fills whatever length it is given
  and does not stop early, so a length longer than the lyrics played the last minutes as wandering
  music. The music writer now sizes the length to its words (about 7 seconds a sung line, about 3 a
  rapped one), and the check before a render names a length that leaves more than a minute after the
  last line, or cuts the words off, with a length that fits.
- MiniMax-H3 no longer reaches for the nvfp4 text encoder first. With both encoder files on a lane
  it now uses the int8 one, which any card can run (nvfp4 needs a GPU with FP4 support); the
  "Text encoder override" field still picks either. A lane with one encoder is unchanged.
- A job is labelled with the engine it actually runs. Every Sound job said "ACE-Step 1.5" (and
  every H3 video "LTX-2.5"), because the label was the lane's first engine, not the job's.
- Each finished History row now has its own small "Remove from History" button (hover or
  keyboard focus reveals it; always on at phone width), so removing a bad result no longer
  requires selecting the row first and finding the Monitor's own button, which was renamed
  "Forget" -> "Remove" for the same reason ("I cannot delete anything I have generated from the
  interface" -- the control existed, but two steps and one unlikely word away). Both use the
  same two-click confirm; the file itself still stays where the lane saved it.
- A History row's title no longer wraps a 250-450 word caption into 15-20 lines and pushes
  every other row far down the list. It clamps to two lines; the full text still reaches a
  hover, in the row's own title attribute.
- The label above the prompt box now names the field it is really bound to (e.g. "Style /
  genre" for a song, instead of the constant "Prompt"), and "Help me write this" now says which
  fields it fills for the current mode (e.g. "Writes: Style / genre, Lyrics"), so a mode whose
  writer also fills a field further down the form -- Lyrics, for YuE2 -- doesn't look like it
  only writes the box it sits under.
- Switching engines inside a room now resets every field to the new engine's own default, except
  a field you actually typed or chose since the last switch -- which now survives one switch
  instead of silently resetting to blank when the new mode happens to reuse the same field id.
  The room also remembers the last engine you picked and restores it the next time you open that
  room (or reload), instead of always defaulting back to the first one.
- The room tab row now scrolls properly at phone width instead of the Cutting Room button
  floating on top of whatever tab happened to be underneath it; every tab is reachable by
  scrolling to it.
- Time estimates now expire. They used to be the all-time median of every finished job, so one
  slow first-load job (or a runtime change that made a mode faster) could stay baked into the
  number indefinitely -- Music3 was showing "about 12 min" from three-day-old jobs long after
  it had settled to 1-3 minutes. Estimates now use only the 5 most recent finished jobs from the
  last 72 hours, and a few hardcoded "measured on this hardware" times in the Video (LTX) pack's
  own text (which could contradict the live number right next to them) were rewritten to
  describe the setting instead of a time or a piece of hardware.

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
- The Film Room Guide's suggestions are buttons under its reply: add the beats it wrote
  to the script (each with its own shot), open or make a shot, put a picture shot in the
  REF ROOM, cut the sequence. Nothing runs until you click, and a button that can't run
  yet says why.
- Cutting Room sequences can be renamed (click the name) and deleted (click twice; the
  file is moved aside, never erased, and every take stays in History). Each row in the
  list shows a small picture of the first picked take, the first beat, the shot count
  and the exact date and time.
- A shot's picture field has "Choose a picture you made": this film's pictures, its REF
  ROOM and History, as thumbnails. A finished picture offers "Use as starting picture",
  which puts it into the next video shot that needs one. A line by the timeline says what
  the small circles on a video shot are for.
- An empty shot has a × to remove it, and any shot has "Delete this shot" (it asks again
  when the shot has takes).

### Changed

- The guide panel's "What the brain was asked" disclosure is gone; the preview of the
  fields stays.
- The three columns now share width in proportion so the history and the form get real
  room on wide screens, while small screens keep the sides usable.
- Pixel Art shows the post-step sprite (scaled with square pixels) as the result; the raw
  render is one click away as "Before the pixel step".
- A song with words but no voice now asks before rendering instead of producing a silent
  instrumental.
- A sequence started with no title is named from the date, then from its first beat,
  until you name it yourself.
- In a sequence, a picture shot starts at the film's own shape (16:9 by default) instead
  of square, and a sound shot starts at the length of the cut so far instead of the
  song recipe's 150 s. Both say so in the form.
- Before a video shot renders, a starting picture of a different shape is pointed out
  (with the engine's own warning), and a cabled picture that will be cropped says so.
- The notes under a video shot about the set plate now say what they mean for that shot
  and offer the fix: use a picture as the set plate, or start the shot from the set
  plate or from its own picture.
- "Write this shot" now marks its own beat as the one to write and sends the previous
  shot's prompt plus the subjects, props and light to keep, so a film's shots stay on
  the same subject in the same light.
- A shot you open with "+ generate here" and never touch is not kept, and coming back to
  the Cutting Room reopens the sequence you left.

### Fixed

- Sending a lane as a list or object now gives a plain error instead of a 500 crash.
- Pixel Art's palette step works on Pillow older than 9.1.
- The H3 check no longer flags the vendor's own task-prefix bracket as a problem; other
  brackets are still caught.
- A helper that thinks before it answers (a reasoning model) no longer fails every writer
  with "didn't come back in the expected shape" or leaves an empty guide bubble when it
  spends its whole budget thinking: the app asks once more with four times the room, then
  says plainly what to change. The new `helper.max_tokens` config key gives every guide
  reply at least that much room; it never lowers a guide's own budget.
- The "Not right? Tell the guide" fixers no longer open with "I can't see the picture" when
  the helper can see it: the text-only rules are sent only to a helper that cannot see.
- Reopening a Cutting Room shot no longer shows its picture as "none yet" and then
  overwrites it on the next save.
- The Film Room Guide no longer shows its beats twice, and the REF ROOM role picker and
  the music bed line ("No vocals..") read properly.
- Help has a section on the room's guide: writing with it, the preview, Compact/Verbose,
  "Describe this picture", "Not right?", "Edit this result", and what shows with no helper.
- A process lane's list of what is missing names the programs it needs (Blender, ffmpeg),
  never GPU model files.
- In the Cutting Room, two quick edits in a row (removing a shot, then pressing "+ generate here" at
  once) no longer lose the second one with "This sequence changed elsewhere": every edit to the
  open sequence now waits its turn and goes out with the current revision.
- A turntable (process-lane) job naming a file that was never uploaded is refused when you
  press Make, not as a failed job later.
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
- A guide write or fix cut off by the helper's length limit is asked for again with more
  room, and if it is still cut off it comes back with a problem saying so, never as a clean
  preview.
- The test suites that need Pillow print a plain SKIP without it instead of crashing, and
  `scripts/run-tests.sh` uses the repo's `.venv` when there is one. Two suites that compare
  against the committed code skip that comparison in a download with no git history,
  instead of failing.
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
