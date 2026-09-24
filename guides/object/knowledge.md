# 3D-room craft knowledge — for the Object Room Guide

Rule-per-line, grouped by the moment in Black Wire Forge (BWF) where it applies: source picture →
how paid tools compare → mesh's own settings → turntable's own settings → judging the result.
Each line ends labelled **observed** (read at the cited source, in-house code/docs or a primary
vendor page), **inferred** (my read connecting two observed facts, or general craft knowledge held
with confidence but not pulled from one citable passage), or **documented (third-party)** (a named
vendor's own claim, restated and attributed, not independently verified). Terms in `code font` are
the app's own words — the guide must speak in these.

## THE ROOM'S SHAPE — one input, two engines, no words

- The `3d` room ("3D — a picture turned into an object you can walk around") takes a **picture**,
  not a prompt — its `mesh` mode has exactly one field, `image_filename` ("Picture", type `image`,
  tier `primary`), and its other mode, `turntable`, takes a `.glb` upload plus render settings (see
  TURNTABLE below). There is no text prompt field anywhere in this room, in either mode. — observed,
  `engines/mesh3d.py` `ENGINE["fields"]["mesh"]`, `engines/turntable.py` `ENGINE["fields"]["turntable"]`,
  `rooms.json`.
- Two engine packs share the room's `3d` capability but do two different jobs: `mesh3d`'s `mesh`
  mode (TRELLIS2, a ComfyUI lane) turns one picture into a textured `.glb`; `turntable`'s
  `turntable` mode (a `process` lane — Blender + ffmpeg on the CPU, no GPU, no ComfyUI) turns an
  existing `.glb` into an orbiting video. They are sequential, not alternatives: a user normally
  runs `mesh` first, then feeds its `.glb` output into `turntable`. — observed,
  `engines/mesh3d.py`, `engines/turntable.py` `ENGINE` dicts.
- Because this room takes a picture, not words, the Guide's main job is **not** writing what
  happens on this screen — it's helping the user get a good source picture, which is made in a
  *different* room (Picture). The Guide must say this plainly and tell the user to go there,
  never pretend it can hand the picture over itself — by design, this is how the room's persona
  works, and it's also just what the field list shows: `mesh3d`'s only field is an already-existing
  image, there is nothing to type here. — observed, `engines/mesh3d.py` `ENGINE["fields"]["mesh"]`.

## SOURCE PICTURE — what TRELLIS2 actually needs, before it ever sees the app

- TRELLIS.2-4B (Microsoft, MIT licence) is a single-image-to-3D model: one picture in, one
  textured mesh with PBR materials out. The public model card and repo describe the input as a
  single image and do not document a required resolution or format beyond that. — observed,
  https://huggingface.co/microsoft/TRELLIS.2-4B and https://github.com/microsoft/TRELLIS.2.
- TRELLIS.2's own README (fetched directly) documents single-image usage only
  (`pipeline.run(image)`) and doesn't mention multi-image conditioning at all — whether an
  experimental multi-image mode exists anywhere in TRELLIS's wider lineage is unconfirmed from the
  TRELLIS.2 repo itself (an earlier claim citing "stochastic fusion/multidiffusion" as an observed
  TRELLIS.2 feature didn't hold up on a direct re-check and has been dropped as unsourced). What's
  certain either way: this app's `mesh3d` pack only ever wires up one image field, so the practical
  point stands regardless — a user cannot send more than one picture to `mesh` in this app. —
  observed, https://github.com/microsoft/TRELLIS.2 (README, direct fetch); observed,
  `engines/mesh3d.py` `ENGINE["fields"]["mesh"]`.
- **BWF already removes the background and crops for you** — the `mesh` graph's own first three
  nodes are `LoadImage` → `RemoveBackground` (the same background-removal model the Clean-up room
  uses) → `ImageCropToMask` (crop to the detected object, padded, 1024×1024, on a black canvas). So
  "plain background" advice matters less here than on a raw TRELLIS install — the pack does that
  step for the user. What the crop-to-mask step *can't* fix: a picture where the background-removal
  model can't cleanly separate the object (camouflage colours, an object the same colour as its
  backdrop, another object touching or overlapping it), or a picture where the object isn't
  actually the biggest/clearest thing in frame. — observed, `engines/mesh3d.py` `mesh_graph()`
  nodes "1"-"4".
- What still matters because BWF's own crop can't invent it: **one object only** (a second object
  in frame gets cropped in or out unpredictably, and TRELLIS.2 itself has no documented multi-object
  handling), the **whole object visible** (a crop can't recover geometry that was never in the
  photo — an object cut off at the frame edge stays cut off in the mesh), and a **3/4 view** rather
  than a flat, perfectly front-on or perfectly side-on shot — a three-quarter angle shows two faces
  of the object at once and gives the model real depth cues a single flat face can't. — inferred
  (general single-image-to-3D practice, several vendor tutorials converge on this, not TRELLIS-
  specific), e.g. https://docs.3daistudio.com/3d-generation/image-to-3d and
  https://www.meshy.ai/tutorials/image-to-3d-model-complete-guide.
- Even, non-directional light on the object itself: a photo lit by one hard flash or strong side
  light bakes a shadow and a highlight into the *picture*, and because TRELLIS.2 generates a
  texture from what it sees, that fake shadow can end up baked into the mesh's texture as if it
  were paint — a flat, cloudy-day or softbox kind of light avoids this. — inferred, general
  photogrammetry/image-to-3D practice, not a claim about TRELLIS.2's internals specifically.
- Documented failure cases for a first TRELLIS attempt, gathered from vendor tutorial guidance (not
  the primary Microsoft card, so treated as inferred/documented rather than observed-at-source):
  very thin parts (stems, wires, thin handles, hair, chains — hard to infer from one view and easy
  to lose or fuse in the mesh), transparent or reflective materials (glass, mirrors — the model has
  no view of what's behind/through them), multiple or tangled objects, severe cropping, motion
  blur, and heavy occlusion (the object partly hidden behind something else in the photo). — cited,
  https://trellis-2.com/blog/trellis-2-complete-tutorial-image-to-3d-guide-2026 (third-party
  tutorial site, not Microsoft's own docs — label this to the user as "the model tends to struggle
  with X" rather than a guaranteed failure).
- A higher-resolution source photo holds up better through the crop-to-mask step than a small or
  heavily-compressed one, since the pipeline's own crop is fixed at 1024×1024 and upsamples a
  smaller source to get there. Tripo AI's own tutorials recommend 2048×2048px or higher as the input
  target for its image-to-3D pipeline (a different model, but the same underlying reason: more
  source detail survives a fixed internal resize) — worth citing to a user as "aim high, 2048px if
  you can" rather than treating BWF's 1024px crop size as the target resolution to shoot for. —
  inferred, general image-to-3D practice plus the pipeline's own fixed crop size
  (`engines/mesh3d.py` node "4", `width`/`height`: 1024); documented (third-party),
  https://www.tripo3d.ai/tutorials/tripo-ai-3d-model-pro-tips.
- **Where the source picture actually gets made**: the Picture room (`picture`, capability
  `image`), whose `t2i` mode has one `prompt` field (type `textarea`) and whose `edit` mode adds
  `ref_images` for working from pictures already made. The Object Guide can write the words for
  that prompt but cannot fill the field itself or generate the picture — the user has to go to the
  Picture room, paste the prompt in, and press Make there. — observed, `engines/qwen_image.py`
  `ENGINE["fields"]`, `rooms.json`.

## HOW PAID IMAGE-TO-3D TOOLS GUIDE USERS — a short survey, borrow vs don't

Checked four paid/commercial image-to-3D tools for how they coach a user's input photo and how they
handle a bad result, to see what's worth borrowing for this room versus what doesn't transfer
because BWF's `mesh` mode doesn't have the control they're advising about. All are third-party
vendor docs, not academic sources — cited as **documented (third-party)**, and none of their prompt
or UI text is copied verbatim (licence caution), only the underlying advice, restated.

- **Meshy**: clear subject, plain background (PNG with transparent/solid background recommended),
  object filling most of the frame without cropping, ≥1024px (2048px+ if using its Refine mode). —
  documented, https://www.meshy.ai/tutorials/image-to-3d-model-complete-guide,
  https://docs.3daistudio.com/3d-generation/image-to-3d.
- **Tripo3D**: 2048×2048px+ input, uniform/diffuse lighting (explicitly warns against harsh shadows
  and direct flash "confusing depth estimation"), a single centred subject on a clean background;
  and a concrete, cited number worth passing on directly — **2-4 images from different angles can
  improve model completeness by over 40% versus one image**, because a single image leaves real
  occlusion gaps by construction, not a bug to fix. — documented,
  https://www.tripo3d.ai/tutorials/tripo-ai-3d-model-pro-tips.
- **Rodin (Hyper3D)**: accepts one image but up to five, and states plainly that **the first image
  supplied is the one used for material/texture generation** when several are given — an interesting
  design choice (image order matters, not just image content) that has no equivalent in BWF's
  single-image field. It also offers a "fidelity" control trading exact-silhouette adherence against
  letting the model fill in occluded detail more freely. — documented,
  https://fal.ai/models/fal-ai/hyper3d/rodin, https://docs.hyper3d.ai/en/api-specification/rodin-gen2-5.
- **CSM (Common Sense Machines)**: uploads an image (ideally already background-free) and states the
  honest limit plainly — "when reconstructing from the front, only assumptions for the back are
  possible," and that it's best suited to organic/simple geometric shapes for that reason. —
  documented, https://3druck.com/en/programs/csm-ai-tool-3d-models-from-2d-images-14120595/.

**Borrow, adapted to what BWF actually has:**
- Tripo's 40%-completeness number is worth citing when a user asks "why can't the model see the
  back" — it's a real, quantified reason a single-image tool leaves gaps, not just this app being
  worse than a multi-image one. Cite it as Tripo's own measurement, about a different model, not a
  TRELLIS.2 number.
- CSM's plain framing of the front/back limit ("only assumptions for the back are possible") is
  exactly the honesty this room's Guide should model — restated in the Guide's own words in "WHAT I
  CAN'T JUDGE" below, not copied.
- 2048px as an input-resolution target (see SOURCE PICTURE above) — the underlying reason (surviving
  a fixed internal resize) transfers even though BWF's fixed size (1024×1024) differs from Tripo's.

**Don't copy — not a real control in this app:**
- Multi-image input, image ordering for texture/material (Rodin), and a fidelity/silhouette-strictness
  slider (Rodin) — `mesh3d`'s field list is exactly one image, full stop; the Guide must never imply
  a second image or a fidelity knob exists here. If a user asks for either, the honest answer is that
  this app's `mesh` mode takes one picture and has one tier — the picture itself is the only lever.
- "Ideally already background-free" (CSM) — don't tell a BWF user to pre-remove the background
  themselves; this app already does that step automatically (see SOURCE PICTURE above), and asking
  the user to duplicate it is wasted work, not better practice here.

## MESH — the `mesh` mode itself, and what the Guide can actually say about it

- `mesh` has exactly one tier, called "Standard" in the app — there is no draft/high quality axis
  to trade, because the underlying graph builder exposes no such knob. If a user asks "can I make
  it faster/higher quality," the honest answer is that this mode doesn't have that dial; the
  picture and the turntable's own settings are what's left to change. — observed,
  `engines/mesh3d.py` `ENGINE["quality"]["mesh"]` (one entry, `"standard"`, `"why": "the only
  setting there is"`).
  a seed exists (`p["seed"]`) but is not exposed as a named field in `ENGINE["fields"]`, so the
  Guide should not claim the user can pick or repeat a specific seed from the UI. — observed,
  `mesh_graph()` signature vs `ENGINE["fields"]["mesh"]`.
- Output is a `.glb` file, not a picture — the mode's own note says so plainly
  ("gives a .glb file, not a picture"). A user expecting a rendered image from this room should be
  told it's a 3D file instead, viewable in the app and ready to feed into `turntable` or export
  elsewhere. — observed, `engines/mesh3d.py` `ENGINE["mode_notes"]["mesh"]`.
- Under the hood the pipeline runs three TRELLIS2 stages in sequence — shape, then texture, over a
  cropped/background-removed version of the source image — but none of these stages are separately
  controllable fields in this app; the Guide should not invent a "shape strength" or "texture
  detail" slider that doesn't exist. — observed, `mesh_graph()` nodes "8"-"22" (all fixed
  parameters, no corresponding field entries).

## TURNTABLE — orbiting a finished `.glb`, on the processor, not the graphics card

- `turntable` is a **process lane**, not a ComfyUI lane — Blender (Cycles) renders N frames on the
  CPU only, never the GPU, and ffmpeg encodes them into `turntable.mp4`; `poster.png`
  (frame 1) is rendered on a transparent film so it keeps alpha even when the video's own background
  is opaque. — observed, `engines/turntable.py` module docstring, `ENGINE["mode_notes"]`.
- Fields the Guide can actually name, all real: **3D model** (`model`, a `.glb` upload — this is
  where a `mesh` mode's output, or any other `.glb`, goes in), **Frames** (one full turn, 24-240,
  default 72 — more frames makes the spin smoother, not longer in time, since fps is separate),
  **Size** (a *select*, not a free number: 384/512/768/1024px square only), **Background** — this
  sets the *video's* own backdrop, not the poster's: `dark` is a near-black studio world
  (`(0.02, 0.02, 0.025)`), `light` is a light-grey studio world (`(0.8, 0.8, 0.8)`), and
  `transparent` renders the video's own world as transparent in Blender — but the video file has no
  alpha channel once it's ffmpeg-encoded, so a transparent-background video ends up rendered on
  black anyway. The *poster* image is a separate render that always keeps real transparency, on
  every setting — it doesn't depend on this field at all. Also: **Frame rate** (12-60 fps),
  **Render samples** (8-256 — more cleans up noise/grain but costs seconds per frame), **Camera
  height angle** (elevation, -10° to 60°, how high the camera sits above the turntable). — observed,
  `engines/turntable.py` `ENGINE["fields"]["turntable"]`, `engines/blender/turntable.py`'s
  "background / world" section (lines ~177-183) for the exact world colours and the poster's
  always-transparent film.
- Three built-in quality tiers, each a bundle of the fields above, not independent knobs to mix
  freely: **Draft** (48 frames, 384px, 16 samples — about half a minute on a simple model),
  **Standard**, the default (72 frames, 512px, 32 samples — a smooth turn at a readable size), and
  **High** (120 frames, 768px, 64 samples — slow and sharp, minutes on a detailed model). — observed,
  `engines/turntable.py` `ENGINE["quality"]["turntable"]`.
- Real measured cost, so the Guide can set expectations honestly rather than guessing: on the
  reference box, a simple factory scene at 32 samples measured 0.92s/frame at 512px and 3.01s/frame
  at 1024px; a real, detailed mesh costs more than the factory scene did. The app's own per-frame
  timeout budget (30s at or under 512px, 60s above 512px) is a ceiling built in for that reason, not
  an estimate of typical render time. — observed, `engines/turntable.py` (`_PER_FRAME = (30, 60)`,
  `budget = _PER_FRAME[0] if size <= 512 else _PER_FRAME[1]` — 512px itself gets the 30s budget) and
  module docstring (measured 2026-09-22 on the reference box).
- Why Cycles runs on the CPU at all, not a plausible-sounding guess: the pack's own code sets it
  deliberately (`scene.cycles.device = "CPU"`, with a fallback that disables GPU compute entirely)
  rather than defaulting to it — slower per frame than a GPU render would be, but it means this mode
  runs on any machine BWF is installed on, with no GPU requirement at all. — observed,
  `engines/blender/turntable.py` (the render-setup section, "Cycles, CPU only").
- The camera is fixed on a circle at the chosen elevation, tracking the model's centre, with three
  lights (key above-front-left, fill front-right, rim behind) — there is no lighting field the user
  can adjust beyond `background` and `elevation`; the Guide should not suggest changing "the light
  colour" or "the key light angle," since no such field exists. — observed,
  `engines/blender/turntable.py` §"studio: key/fill/rim" comment.
- The model is auto-imported, bounded, centred and scaled to fit the frame before the camera orbit
  starts — the user does not need to worry about the `.glb`'s original scale or origin point; the
  app handles that. — observed, `engines/blender/turntable.py` §"import, bound, centre, scale".

## NOT RIGHT? — diagnosing a finished mesh or turntable, from a still plus a complaint

This is the Guide's second skill: the user shows a still (a poster frame, or a screenshot of the
viewer) and says what's wrong; the Guide's job is to say whether the fix belongs in the **source
picture** (re-shoot or re-generate it in the Picture room) or in **turntable's own settings**
(a re-render with different fields, no new source picture needed), and to say plainly when it can't
tell from a still alone.

| complaint | likely cause | where the fix belongs |
|---|---|---|
| a part is missing, thin, or fused into the body (a handle, a stem, a strap, hair, wire) | thin geometry is hard to infer from one view — a known TRELLIS limitation | **source picture**: re-shoot/re-generate so that part reads clearly and isn't foreshortened; a 3/4 view that shows the thin part's full length helps more than a flat front view |
| a second, unwanted lump or blob of geometry appears where the background used to be, or the object looks doubled | the background-removal step didn't cleanly separate the object (colour too close to the backdrop, another object touching it in the photo), or the photo actually had two objects in it | **source picture**: a plainer, higher-contrast background, or a re-crop so only one object is in frame |
| the mesh looks flat, warped, or "melted" on one side | that side of the object was never visible in the source photo — a single image can't show what it never saw | **source picture**: a 3/4 angle that shows more of that side, since this app's `mesh` mode takes exactly one image, not several views |
| the texture has a baked-in shadow or hot spot that doesn't move when the model turns | the source photo had strong directional light or a hard flash, and the texture stage painted that lighting onto the surface | **source picture**: re-shoot/re-generate with flatter, more even light |
| the model looks fine, but the turn is jerky or grainy | too few frames or too few render samples for `turntable` | **turntable's own settings**: raise Frames and/or Render samples (the High preset, or a custom bump), no new source picture needed |
| the turntable render is very slow or times out | Size and/or Frames set high on a detailed mesh, on CPU-only Cycles | **turntable's own settings**: drop Size and/or Frames (the Draft preset is the fast option), or accept the wait — this app renders turntables on the CPU only, no GPU path exists |
| "does it look like the picture" / "is the face/logo/label right" | likeness and small-detail judgment | **the Guide cannot judge this from a still** — say so plainly and ask the user to look at the model themselves, full size, from more than one angle |
| a request with no still attached — "it doesn't look right" | no way to diagnose without seeing it | ask the user to describe what's wrong, or attach a still, rather than guessing at a cause |

- If the still shows the mesh from only one angle, that's a real constraint on the Guide's own
  answer — a defect that's actually on the far side of the model won't be visible in a single
  still, and the Guide should say so rather than declare the model "fine." — inferred, general
  honesty-about-limits carried over from the Film Guide's own vision-boundary design.

## WHAT I CAN'T JUDGE — SAY SO, DON'T GUESS

- Whether a mesh or texture actually "matches" a real, identifiable person or a specific real
  object's likeness — the Guide never claims to judge or reproduce a real person's likeness, in
  words or in a rendered model. — by design: this guide never judges a real person's or
  object's likeness.
- **The mesh's back (or any side not shown in a still)**: a single source photo only ever showed the
  model one side, and a still of the finished mesh only ever shows one more angle on top of that —
  the Guide has no more access to the unseen side than the user does. This isn't a BWF weakness
  specifically; even a paid multi-view tool (CSM) states the same limit for a single-image input —
  "only assumptions for the back are possible." — observed (BWF's own single-image field,
  `engines/mesh3d.py`), reinforced by documented (third-party) CSM framing above.
- **Whether the mesh is watertight** (a fully sealed, hole-free surface, no gaps or open edges) — the
  Guide has no way to inspect topology from a rendered still or a turntable video; it can only relay
  what's documented about the model in general (TRELLIS.2's own card notes generated meshes "may
  contain small holes or minor topological discontinuities" and that hole-filling is a separate,
  optional post-process this app doesn't run) rather than assert a specific mesh is or isn't sealed.
  — observed, https://huggingface.co/microsoft/TRELLIS.2-4B.
- **Whether the mesh is print-ready** (3D-printable without further repair — wall thickness, manifold
  geometry, scale for a specific printer) — this app doesn't run any print-prep step, and the Guide
  has no view into a slicer's own checks; the honest answer is that a `.glb` from this room is a
  visual/turntable asset, not verified print-ready, and printing it would need a separate repair
  tool the Guide can't run or judge here.
- Whether a defect visible in a still is present on a side of the model the still doesn't show.
- Whether TRELLIS2's own internal seed or sampling produced a specific artifact — the app exposes
  no seed field for `mesh`, so there's nothing for the Guide to point at or vary there.
- Whether the source photo's background-removal actually succeeded cleanly, beyond what's visible
  in the resulting mesh or a still of it — the Guide only sees the outcome, not the mask itself.

## RESEARCH LOG

**In-house sources checked first**, before any web search: `engines/mesh3d.py` and
`engines/turntable.py` (the pack code itself, primary source for every field/graph claim above,
including why Cycles runs CPU-only — set explicitly in `engines/blender/turntable.py`'s render
setup, not assumed); `docs/MODELS.md`'s TRELLIS2 and Blender sections (model
roles, licence, file-discovery match, confirms the `engines/blender/turntable.py` pack). No other
in-house write-up specific to this room's craft was found beyond what's already cited in the pack's
own docstring and `MODELS.md` — this room's craft knowledge is otherwise new territory, not
something being re-derived from an existing note.

**Then the web**: the TRELLIS.2-4B Hugging Face model card and the microsoft/TRELLIS.2 GitHub repo
(fetched directly, both **observed** above — MIT licence, single-image input, no documented
resolution requirement, "field-free" O-Voxel structure, PBR materials including opacity, GLB
exported opaque by default, "small holes/minor topological discontinuities" as the model's own
documented limitation). Also searched for TRELLIS-specific failure modes; the clearest documented
list (thin/transparent/tangled objects, multiple objects, severe cropping, motion blur, heavy
occlusion) came from a third-party tutorial site (trellis-2.com), not Microsoft's own docs, so it's
cited as **documented (third-party)** rather than **observed** at the primary source. General
image-to-3D best-practice guidance (3/4 view, plain background, even light, whole object in frame,
resolution) came from several vendor docs (3D AI Studio, Meshy, Tripo3D, Rodin, CSM — see "HOW PAID
IMAGE-TO-3D TOOLS GUIDE USERS" above) that converge on the same handful of points across different
underlying models — treated as **inferred**, general craft knowledge, not TRELLIS-specific, except
where a vendor's own stated number (Tripo's 2048px target, its 40%-completeness figure for
multi-image) is quoted and attributed as that vendor's own claim, not generalized as fact about
TRELLIS.2.

**Could not confirm**: any TRELLIS.2-specific published benchmark of exactly how much a 3/4 view
vs a flat front view changes output quality (no controlled study found, only converging tutorial
advice); whether BWF's own background-removal model (BiRefNet, shared with the Clean-up room) has
different failure characteristics than TRELLIS's own examples assume; whether TRELLIS.2's own
single-image completeness gap is comparable in size to Tripo's cited 40% figure (that number is
Tripo's own model, not TRELLIS.2 — cited as an illustrative, not transferable, measurement).
