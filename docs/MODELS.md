# Getting the models

Black Wire Forge ships **no model weights**. Every row below is something *you* download,
under *that model's own licence* — this app neither holds nor relicenses those terms.

**Before downloading anything: tell the user the file size and licence and get an explicit
yes** (AGENTS.md, "Rules for the agent"). Commercial revenue thresholds, non-commercial
licences, display requirements, and territory restrictions are stated in the "Licence" line
under each model's own heading, and restated together under "Licence gates" at the end.

**Provenance rule (hard):** a row may recommend a file only if it is a file **we have actually
run** — proved by a byte-size match against the copy on a working ComfyUI install's own
`models/` tree (checked directly with `stat`, 2026-09-24). A file that install has never
run is marked **"not run by us"** and is not the recommended
download, even when its Hub listing looks like a plausible match. Every "Source" repo/file was
also confirmed live on the Hugging Face Hub (repo exists, file exists, size as shown) with the
`hub_repo_details` / `hf_fs` MCP tools. Anything that could not be confirmed live says so
instead of a URL. For GitHub node packages, existence was checked with `gh repo view`.

**Discovery match**: each pack's `roles` dict (`engines/<pack>.py`) tells `server.py`'s
`pick_model()` which filenames it will accept for a role (`engines/__init__.py`'s
`role_rules()`). "PASS" means the exact upstream filename, dropped straight into the matching
ComfyUI folder, satisfies that rule as-is. "FAIL bare" means it does not, and the note under
the table gives the exact subfolder or rename that makes it pass. Every PASS/FAIL claim was
checked by running `pick_model()` itself against a one-file synthetic pool per role, not by
reading the rule and guessing.

## Qwen-Image 2.1 (`engines/qwen_image.py`; also required by `engines/pixelart.py` -- see its
note below)

Licence: **Qwen RESEARCH LICENSE AGREEMENT** (non-commercial) — [licence text](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE). Attribution: Qwen-Image 2.1 by Alibaba Qwen team.

| Role | Source (HF repo · file) | HF size | Verified size | Match | ComfyUI folder | Discovery match |
|---|---|---|---|---|---|---|
| `qwen_unet` | `Abiray/Qwen-Image-2.1-GGUF` · `qwen_image_2.1_Q6_K.gguf` | 5,876,578,464 | 5,876,578,464 | yes | `models/unet` (`UnetLoaderGGUF`) | PASS — contains `qwen_image` |
| `qwen_clip` | `Comfy-Org/Qwen-Image-2.1` · `text_encoders/qwen3vl_8b_int8_convrot.safetensors` | 9,350,798,360 | 9,350,798,360 | yes | `models/text_encoders` (`CLIPLoader`) | PASS — contains `qwen3vl`, `8b` |
| `qwen_vae` | `Comfy-Org/Qwen-Image-2.1` · `vae/qwen_image_2.1_vae_bf16.safetensors` | 675,509,688 | 675,509,688 | yes | `models/vae` (`VAELoader`) | PASS — contains `qwen_image`, `vae`, `2.1` |

Verified working source: `Abiray/Qwen-Image-2.1-GGUF`'s `qwen_image_2.1_Q6_K.gguf`
(5,876,578,464 bytes) — not `leejet/Qwen-Image-2.1-GGUF`'s `qwen_image_2.1-Q6_K.gguf`
(5,996,851,232 bytes), a different build that does not size-match a working install.

**Also required: Pixel Art mode.** `engines/pixelart.py` reuses these same three roles plus
BiRefNet (`birefnet_model` — see the BiRefNet section below); downloading only the three rows
above leaves Pixel Art unavailable.

## LTX-2.5 (`engines/ltx.py`) — video

Licence: **LTX-2.x Community License** ([official text](https://raw.githubusercontent.com/Lightricks/LTX-2/main/LICENSE-2_x)), confirmed live 2026-09-24. Section 2.1: *"Entities with annual
revenues of at least $10,000,000 (the 'Commercial Entities') are required to obtain a paid
license for any use"* — anyone under that threshold uses it free under the Community terms;
$10M+/year commercial entities need a separate paid licence from Lightricks. Attribution:
LTX-2.5 by Lightricks.

| Role | Source (HF repo · file) | HF size | Verified size | Match | ComfyUI folder | Discovery match |
|---|---|---|---|---|---|---|
| `ltx_transformer` | `ruygar/LTX-2.5-Comfy-GGUF` · `ltx-2.5-22b-distilled-transformer-bf16-Q5_K_M.gguf` | 14,831,573,088 | 14,831,573,088 | yes | `models/unet` (`UnetLoaderGGUF`) | PASS — contains `ltx`, `gguf` |
| `ltx_clip` | `comfyicu/LTX-2.5` · `text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors` | 15,372,971,786 | 15,372,971,786 | yes | `models/text_encoders` (`CLIPLoader`) | PASS — contains `ltx` |
| `ltx_vae_video` | `comfyicu/LTX-2.5` · `vae/ltx-2.5-video-vae-bf16.safetensors` | 1,472,223,346 | 1,472,223,346 | yes | `models/vae` (`VAELoader`) | PASS — contains `ltx`, `video`, not `audio` |
| `ltx_vae_audio` | `comfyicu/LTX-2.5` · `vae/ltx-2.5-audio-vae-bf16.safetensors` | 364,866,540 | 364,866,540 | yes | `models/vae` (`VAELoader`) | PASS — contains `ltx`, `audio` |
| `ltx_upscaler` | `comfyicu/LTX-2.5` · `latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors` | 995,778,752 | 995,778,752 | yes | `models/latent_upscale_models` (`LatentUpscaleModelLoader`) | PASS — contains `ltx`, `spatial` |

🔴 The `ltx_upscaler` ComfyUI folder is **`models/latent_upscale_models`**, not
`models/latent_upscaler` — confirmed against ComfyUI's own folder_paths.py, which registers
`"latent_upscale_models"` as the directory `LatentUpscaleModelLoader` reads. A file dropped
into a folder named `latent_upscaler` is invisible to that node.

Verified working source: `ltx_transformer` (`ruygar/LTX-2.5-Comfy-GGUF`) matches as recommended.
The other four roles come from `comfyicu/LTX-2.5`, not `Lightricks/LTX-2.5` directly —
Lightricks' `text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors` is
**2,412 bytes smaller** (15,372,969,374) than the verified working copy and is therefore a
*different build*, not byte-identical; the VAEs and upscaler happen to be byte-identical
between the two repos, only the text encoder differs. Recommend `comfyicu/LTX-2.5` for all
four. A working install may also carry `ltx-2.5-video-vae-conv-bf16.safetensors` (1,452,269,922
bytes, same repo) and a duration-head model patch (3,843,690 bytes) that neither
`engines/ltx.py`'s `roles` dict nor this table currently needs — not fetched for a declared
role, so not listed as a recommendation here. `Lightricks/LTX-2.5` is a **gated** repo;
`comfyicu/LTX-2.5` is not.

## MiniMax-H3 (`engines/minimax_h3.py`) — video

Licence: **MiniMax H3 License Agreement** ([official text](https://huggingface.co/MiniMaxAI/MiniMax-H3/raw/main/LICENSE)), confirmed live 2026-09-24, three clauses:

- Territory (§I.5): *"'Excluded Territories' means the European Union, the United Kingdom, the
  Republic of Korea and the United States of America."* (§V.4 backs this with a use
  restriction: *"You may not use, reproduce, modify, distribute, or display the MiniMax H3
  Works or any of their Outputs or results outside the Applicable Territory."*) Outside those
  four territories the licence is open; inside them, apply to MiniMax first.
- Display (§IV.2): *"You shall prominently display 'MiniMax H3' on the user interface of
  commercial product or service that uses MiniMax H3 or MiniMax H3 Works."*
- Revenue/authorization (§IV.1): *"You shall obtain a separate, prior written authorization
  from MiniMax by contacting api@minimax.io with the subject line 'MiniMax H3 licensing -
  authorization request', if your commercial products and services generate more than 20
  million US dollars (or equivalent in other currencies) in yearly revenue."*

Attribution: MiniMax-H3 by MiniMax.

| Role | Source (HF repo · file) | HF size | Verified size | Match | ComfyUI folder | Discovery match |
|---|---|---|---|---|---|---|
| `h3_unet_fl2va` | `Abiray/MiniMax-H3-Pruned-GGUF` · `MiniMax-H3-FL2VA-Pruned-Q4_K_M.gguf` | 11,564,180,576 | 11,564,180,576 | yes | `models/diffusion_models/minimax_h3/` (`UnetLoaderGGUF`) | **FAIL bare → PASS in `minimax_h3/`** |
| `h3_unet_ref2va` | `Abiray/MiniMax-H3-Pruned-GGUF` · `MiniMax-H3-Ref2VA-Pruned-Q4_K_M.gguf` | 11,564,180,576 | 11,564,180,576 | yes | `models/diffusion_models/minimax_h3/` (`UnetLoaderGGUF`) | **FAIL bare → PASS in `minimax_h3/`** |
| `h3_clip_nvfp4` | `Comfy-Org/MiniMax-H3` · `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 15,687,142,551 | 15,687,142,551 | yes | `models/text_encoders` (`CLIPLoader`) | PASS — contains `qwen3vl`, `minimax_h3`, `nvfp4`, `awq` |
| `h3_clip_int8` | `Comfy-Org/MiniMax-H3` · `text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | 27,141,342,152 | — | **not run by us** — we run the nvfp4 encoder above; alternative only | `models/text_encoders` (`CLIPLoader`) | PASS — contains `qwen3vl`, `minimax_h3`, `int8` (rule check only) |
| `h3_vae_video` | `Comfy-Org/MiniMax-H3` · `vae/minimax_h3_video_vae_fp16.safetensors` | 5,207,808,496 | 5,207,808,496 | yes | `models/vae` (`VAELoader`) | PASS — contains `minimax_h3`, `video`, not `audio` |
| `h3_vae_audio` | `Comfy-Org/MiniMax-H3` · `vae/minimax_h3_audio_vae_fp32.safetensors` | 605,254,808 | 605,254,808 | yes | `models/vae` (`VAELoader`) | PASS — contains `minimax_h3`, `audio` |
| `h3_turbo_lora` (recommended: 4-step) | `Comfy-Org/MiniMax-H3` · `loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors` | 1,956,192,992 | 1,956,192,992 | yes | `models/loras/minimax_h3/` (`LoraLoaderModelOnly`) | PASS — contains `minimax_h3`, `4step`, `comfy`, `fl2v` |
| `h3_turbo_lora` (alternative: 8-step, unlicensed) | `TenStrip/MinimaxH3-Turbo_Shenanigans` · `lightx2v_hybrid-4to8step-Turbo_r48.safetensors` | 943,947,204 | 943,947,204 | yes | `models/loras/minimax_h3/` (`LoraLoaderModelOnly`) | **FAIL bare → PASS in `minimax_h3/`** |

**`h3_turbo_lora`: the speed pack is optional.** Neither `fl2va` nor `ref2v` needs it to run —
it only feeds the pack's fast-quality tiers (`turbo` ability, `engines/minimax_h3.py`'s
`cap_from`/`provides`). Recommended default is `Comfy-Org/MiniMax-H3`'s 4-step LoRA, which
carries the base MiniMax-H3 licence above and no extra gate. The 8-step alternative
(`TenStrip/MinimaxH3-Turbo_Shenanigans`) was measured clearly better on our tests; its author
states no licence for the LoRA itself, so **ask them before using it** — do not tell an agent
to download it without that caveat reaching the user.

Also fixed, two more roles: `h3_unet_fl2va`/`h3_unet_ref2va` come from
`Abiray/MiniMax-H3-Pruned-GGUF`, not `unsloth/MiniMax-H3-GGUF` (a different, non-matching
build). Its filenames are hyphenated/mixed-case — `MiniMax-H3-FL2VA-Pruned-Q4_K_M.gguf` — which
does **not** contain the lowercase, underscored substring `minimax_h3` the role rules require:
confirmed **FAIL** by running `pick_model()` against the bare filename. Placed in a
`minimax_h3/` subfolder (`models/diffusion_models/minimax_h3/`), the dropdown-visible path is
`minimax_h3/MiniMax-H3-FL2VA-Pruned-Q4_K_M.gguf`, which does contain `minimax_h3` —
re-running `pick_model()` against that form confirmed **PASS** for both roles. The subfolder
is not cosmetic; it's required. The same subfolder requirement applies to both `h3_turbo_lora`
rows (bare filenames for both the 4-step and 8-step LoRAs lack `minimax_h3` too, at
`models/loras/minimax_h3/`).

## ACE-Step 1.5 (`engines/audio.py`, song mode) — audio

Licence: **MIT**. Attribution: ACE-Step 1.5 by ACE Studio and StepFun.

| Role | Source (HF repo · file) | HF size | Verified size | Match | ComfyUI folder | Discovery match |
|---|---|---|---|---|---|---|
| `ace_unet` | `Comfy-Org/ace_step_1.5_ComfyUI_files` · `split_files/diffusion_models/acestep_v1.5_turbo.safetensors` | 4,787,825,604 | 4,787,825,604 | yes | `models/diffusion_models/ace_step_1.5/` (`UNETLoader`) | **FAIL bare → PASS in `ace_step_1.5/`** |
| `ace_clip1` + `ace_clip2` | `Comfy-Org/ace_step_1.5_ComfyUI_files` · `split_files/text_encoders/qwen_0.6b_ace15.safetensors` + `qwen_1.7b_ace15.safetensors` | 1,191,588,248 + 3,708,523,360 | 1,191,588,248 + 3,708,523,360 | yes | `models/text_encoders/ace_step_1.5/` (`DualCLIPLoader`, both files loaded together) | **FAIL bare → PASS in `ace_step_1.5/`** |
| `ace_vae` | `Comfy-Org/ace_step_1.5_ComfyUI_files` · `split_files/vae/ace_1.5_vae.safetensors` | 337,431,732 | 337,431,732 | yes | `models/vae/ace_step_1.5/` (`VAELoader`) | **FAIL bare → PASS in `ace_step_1.5/`** |

`ace_clip1`/`ace_clip2` are loaded together by a single `DualCLIPLoader` node
(`engines/audio.py`'s song graph), not two separate `CLIPLoader` calls.

All four sizes match a verified working install exactly. **All four still need the same fix.**
Every role's rule requires the literal substring `ace_step_1.5` in the filename ComfyUI shows
in its dropdown. None of the upstream filenames contain it as-is
(`acestep_v1.5_turbo.safetensors`, `qwen_0.6b_ace15.safetensors`, `qwen_1.7b_ace15.safetensors`,
`ace_1.5_vae.safetensors`) — confirmed **FAIL** by running `pick_model()`. **Fix: put all four
files, with their upstream names unchanged, in an `ace_step_1.5/` subfolder** inside the
relevant ComfyUI models directory — this is exactly what a working install already does, and
re-running `pick_model()` against each `ace_step_1.5/<file>` form confirmed **PASS** for all
four.

## MiniMax-Music3 (`engines/audio.py`, music mode) — audio

⚠ The Hub's own metadata tag on `Comfy-Org/MiniMax-Music-3` says `license: apache-2.0`, but the
actual `LICENSE` file in `MiniMaxAI/MiniMax-Music3` (fetched and read directly, confirmed live
2026-09-24) is a custom **"MiniMax-Music3 COMMUNITY LICENSE"**, not Apache-2.0: permissive to
use/copy/modify/distribute, but §3.1 requires *"You shall prominently display 'MiniMax-Music3'
on the user interface of commercial product or service that uses the Software"*, and §3.2
requires separate written authorization from MiniMax above $20M/year aggregate revenue. Treat
the LICENSE file, not the Hub tag, as authoritative. Attribution: MiniMax-Music3.

| Role | Source (HF repo · file) | HF size | Verified size | Match | ComfyUI folder | Discovery match |
|---|---|---|---|---|---|---|
| `music3_unet` | `Comfy-Org/MiniMax-Music-3` · `diffusion_models/minimax_music3_dit_int8_convrot.safetensors` | 2,502,161,682 | 2,502,161,682 | yes | `models/diffusion_models` (`UNETLoader`) | PASS — contains `minimax_music3` (proven: available on a working install) |
| `music3_clip` | `Comfy-Org/MiniMax-Music-3` · `text_encoders/minimax_music3_text_encoder_pruned_int8_convrot.safetensors` | 9,196,611,886 | 9,196,611,886 | yes | `models/text_encoders` (`CLIPLoader`) | PASS — contains `minimax_music3` (proven: available on a working install) |
| `music3_vae` | `Comfy-Org/MiniMax-Music-3` · `vae/minimax_music3_dav.safetensors` | 216,696,128 | 216,696,128 | yes | `models/vae` (`VAELoader`) | PASS — contains `minimax_music3` (proven: available on a working install) |

On the working install these three files live in `minimax_music3/` subfolders of
`diffusion_models/`, `text_encoders/` and `vae/`; the bare names above already match the
rule, so either placement works.

## Stable Audio Open (`engines/audio.py`, sfx mode) — audio

Licence: **Stability AI Community License** ([official text](https://stability.ai/license)), confirmed live 2026-09-24: *"Use of the Core Models ... is free for everyone, unless you're
using the Core Models for a commercial purpose and you or your organization generate over USD
$1M (or local currency equivalent) of annual revenue, regardless of the source of that
revenue"* — over that threshold, an Enterprise licence is required instead. Also requires
visible attribution ("Powered by Stability AI"). Gated repo on the Hub (accept terms before
download).

| Role | Source (HF repo · file) | HF size | Verified size | Match | ComfyUI folder | Discovery match |
|---|---|---|---|---|---|---|
| `sao_ckpt` | `stabilityai/stable-audio-open-1.0` · `model.safetensors` | 4,853,889,016 | 4,853,889,016 | yes | `models/checkpoints/stable-audio-open-1.0/` (`CheckpointLoaderSimple`) | FAIL bare → PASS in `stable-audio-open-1.0/` (proven: available on a working install, where it is saved as `stable-audio-open-1.0.safetensors`) |
| `sao_clip` | `google-t5/t5-base` · `model.safetensors` (Apache-2.0), saved as `t5-base.safetensors` | 891,646,390 | 891,646,390 | yes (SHA-256 `a9090354…` identical) | `models/text_encoders/stable-audio-open-1.0/` (`CLIPLoader`) | FAIL bare → PASS in `stable-audio-open-1.0/` (proven: available on a working install) |

`sao_clip` is loaded by `CLIPLoader` (`engines/audio.py`'s sfx graph), not `AudioEncoderLoader`
— `AudioEncoderLoader` is used elsewhere in this pack, for YuE2's SheetSage2 encoder only.

Both roles' rules require the literal substring `stable-audio-open` in the file's path. Neither
upstream file name contains it (confirmed **FAIL** bare with `pick_model()`), so place them in
`stable-audio-open-1.0/` subfolders: `models/checkpoints/stable-audio-open-1.0/` and
`models/text_encoders/stable-audio-open-1.0/`, which is exactly the working install's layout.

**The text encoder is not the repo's own `text_encoder/model.safetensors`** (438,525,864 bytes,
never run by us). The working install uses Google's full `t5-base` weights, byte-identical
(SHA-256 match) to `google-t5/t5-base` · `model.safetensors`, saved as `t5-base.safetensors`.

## YuE2-3B (`engines/audio.py`, yue2/cover modes) — audio

Licence: **CC BY-NC 4.0** (non-commercial). Attribution: YuE2-3B by the m-a-p project.

| Role | Source (HF repo · file) | HF size | Verified size | Match | ComfyUI folder | Discovery match |
|---|---|---|---|---|---|---|
| `yue2_ckpt` | `Comfy-Org/YuE2` · `checkpoints/yue2_3b_bf16.safetensors` | 7,799,983,228 | 7,799,983,228 | yes | `models/checkpoints` (`CheckpointLoaderSimple`) | PASS — contains `yue2`, not `int8` |
| `yue2_audio_encoder` | `Comfy-Org/YuE2` · `audio_encoders/sheetsage2_bf16.safetensors` | 1,386,868,122 | 1,386,868,122 | yes | `models/audio_encoders` (`AudioEncoderLoader`) | PASS — contains `sheetsage2` |

Both filenames are byte-identical to a working install's copy — no rename needed. An
`int8_convrot` build of the checkpoint (3.96 GB) is also in `Comfy-Org/YuE2` for tighter VRAM,
but is not the one verified here.

⚠ `yue2_audio_encoder` is a SheetSage2 checkpoint, and **SheetSage2 itself
(`m-a-p/SheetSage2`) carries its own licence: CC BY-NC 4.0** (confirmed on the Hub) — not
currently named in README's per-engine licence list. It's the same non-commercial family as
YuE2-3B, so it doesn't change the download decision for cover mode, but it's worth restating:
cover mode pulls in a second CC BY-NC 4.0 model, not just YuE2-3B.

## BiRefNet (`engines/cleanup.py`, cutout mode; also required by Pixel Art and TRELLIS2) — image (tool)

Licence: **MIT**. Attribution: BiRefNet.

| Role | Source (HF repo · file) | HF size | Verified size | Match | ComfyUI folder | Discovery match |
|---|---|---|---|---|---|---|
| `birefnet_model` | `ZhengPeng7/BiRefNet` · `model.safetensors` | 444,473,596 | 444,473,596 | yes | `models/background_removal` (`LoadBackgroundRemovalModel`) | PASS — role rule has no filename requirement |

**This role is shared.** `birefnet_model` is required not only by cutout mode but also by
Pixel Art (`engines/pixelart.py`) and TRELLIS2 mesh generation (`engines/mesh3d.py`) — both
declare it in their own `provides`. Downloading every row under Qwen-Image 2.1 or TRELLIS2
without also downloading this one leaves those modes unavailable.

`Comfy-Org/BiRefNet` also carries the identical bytes under `background_removal/birefnet.safetensors`
if a ComfyUI-branded filename is preferred; either works, since `birefnet_model`'s rule
(`all: []`) accepts any file in the `bg_removal` pool. A working install's own copy lives at
`models/background_removal/model.safetensors`, matching `ZhengPeng7/BiRefNet`'s bare filename
exactly, so that's the recommended one.

## Real-ESRGAN x4plus (`engines/cleanup.py`, upscale mode) — image (tool)

Licence: **BSD-3-Clause** (the licence of the upstream GitHub repository, xinntao/Real-ESRGAN,
as recorded in this pack's own licence entry in `engines/cleanup.py`; Real-ESRGAN's canonical
release is GitHub, not Hugging Face, so no Hub licence tag exists to check against).
Attribution: Real-ESRGAN by Xintao Wang et al.

| Role | Source | HF size | Verified size | Match | ComfyUI folder | Discovery match |
|---|---|---|---|---|---|---|
| `upscale_model` | Official page: [github.com/xinntao/Real-ESRGAN releases](https://github.com/xinntao/Real-ESRGAN/releases) (`RealESRGAN_x4plus.pth`); mirrored on the Hub at `schwgHao/RealESRGAN_x4plus` · `RealESRGAN_x4plus.pth` | 67,040,989 | 67,040,989 | yes | `models/upscale_models` (`UpscaleModelLoader`) | PASS — role rule has no filename requirement |

The Hub mirror's size matches a working install's actual file exactly, so the mirror is
proven against a real copy. The GitHub Releases page was not fetched, so its URL above is
unverified.

## TRELLIS2 (`engines/mesh3d.py`; also requires BiRefNet — see its section above) — 3d

Licence: **MIT**. Attribution: TRELLIS2 by Microsoft.

| Role | Source (HF repo · file) | HF size | Verified size | Match | ComfyUI folder | Discovery match |
|---|---|---|---|---|---|---|
| `trellis_unet` | `Comfy-Org/TRELLIS.2` · `diffusion_models/trellis_2_int8_convrot.safetensors` | 5,253,048,192 | 5,253,048,192 | yes | `models/diffusion_models` (`UNETLoader`) | PASS — contains `trellis` (proven: available on a working install) |
| `trellis_shape_vae` | `Comfy-Org/TRELLIS.2` · `vae/trellis_2_shape_vae_bf16.safetensors` | 1,095,844,024 | 1,095,844,024 | yes | `models/vae` (`VAELoader`) | PASS — contains `trellis`, `shape` (proven: available on a working install) |
| `trellis_texture_vae` | `Comfy-Org/TRELLIS.2` · `vae/trellis_2_texture_vae_bf16.safetensors` | 948,461,364 | 948,461,364 | yes | `models/vae` (`VAELoader`) | PASS — contains `trellis`, `texture` (proven: available on a working install) |
| `trellis_clip_vision` | `Comfy-Org/TRELLIS.2` · `clip_vision/dino_v3_vit_l.safetensors` | 1,212,559,776 | 1,212,559,776 | yes | `models/clip_vision/trellis2/` (`CLIPVisionLoader`) | FAIL bare → PASS in `trellis2/` (proven: available on a working install) |

**Also required: BiRefNet.** `engines/mesh3d.py`'s `mesh` ability lists `birefnet_model`
alongside these four roles — see the BiRefNet section above. Downloading only the rows here
leaves mesh generation unavailable.

On the working install all four files live in `trellis2/` subfolders. `trellis_clip_vision`'s
rule requires the literal substring `trellis` in the path; the upstream file is named
`dino_v3_vit_l.safetensors` (confirmed **FAIL** bare with `pick_model()`), so it must go in a
`trellis2/` subfolder: `models/clip_vision/trellis2/dino_v3_vit_l.safetensors`.

## Blender (`engines/turntable.py`) — 3d (process lane, no ComfyUI model)

Licence: **GPL-2.0-or-later** — covers the Blender program itself, not the pictures/videos you
render with it. Not a Hugging Face download: install Blender from
[blender.org](https://www.blender.org/download/) and have `blender` + `ffmpeg` on `PATH`
(`command -v blender && command -v ffmpeg`); see AGENTS.md's "Install + start" and the
`engines/turntable.py` pack for the `"bins"` it needs.

## Custom-node packages

Every node class not listed here is **core ComfyUI** (`comfy_extras/`, bundled with ComfyUI
itself — nothing to install). Verified live 2026-09-24 by reading the actual node source in
`comfyanonymous/ComfyUI` (comfy_extras/nodes_qwen.py, nodes_apg.py, nodes_fresca.py,
nodes_lt.py, nodes_minimax_h3.py, nodes_minimax_music.py, nodes_yue2.py, nodes_audio_encoder.py,
nodes_bg_removal.py, nodes_trellis2.py, nodes_mesh_postprocess.py, nodes_save_3d.py,
nodes_mesh_io.py) and confirming class names directly, not by assumption — this includes every
`MiniMaxH3*`, `MiniMaxMusic3*`, `YuE2*`, `Trellis2*`/`VaeDecode*Trellis*`, `LoadBackgroundRemovalModel`/
`RemoveBackground`, `AudioEncoderLoader`/`SheetSage2AudioToABC`, `TextEncodeQwenImage21`/`APG`/`FreSca`,
and every `engines/mesh3d.py` post-process/save node (`BakeTextureFromVoxel`, `ApplyTextureToMesh`,
`MeshSmoothNormals`, `UnwrapMesh`, `GetMeshInfo`, `Save3DAdvanced`, `MeshToFile3D`) named in
AGENTS.md's pack table. `LatentUpscaleModelLoader` is core too, per ComfyUI's own built-in node
docs, though it lives outside nodes_lt.py.

**LTX is split between core and a custom pack.** As of current ComfyUI,
`LTXVConditioning`, `LTXVAddGuide`, `LTXVCropGuides`, `LTXVImgToVideoInplace`, `LTXVPreprocess`,
`LTXVDualCFGGuider`, `LTXVConcatAVLatent`, `LTXVSeparateAVLatent`, and `EmptyLTXVLatentVideo`
are core (comfy_extras/nodes_lt.py in current ComfyUI), confirmed by reading that file directly. Only the
looping/context/upsampling and audio-VAE classes `engines/ltx.py` uses remain
ComfyUI-LTXVideo-only: `LTXVContextWindows`, `LTXVLoopingSampler`, `LTXVLatentUpsampler`,
`LTXVAudioVAEDecode`, `LTXVEmptyLatentAudio`.

| Package | Repo | Licence | Provides |
|---|---|---|---|
| ComfyUI-GGUF | [github.com/city96/ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) | Apache-2.0 | `UnetLoaderGGUF` — used dynamically by `engines/__init__.py`'s `unet_loader()` whenever a role's model file ends `.gguf` (`engines/qwen_image.py`/`engines/pixelart.py`, `engines/ltx.py`, `engines/minimax_h3.py`) |
| ComfyUI-LTXVideo | [github.com/Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo) | "Other" (Lightricks; not independently re-read here — README already cites the model's own LTX-2.x Community License) | Only the 5 classes named above that are still NOT core: `LTXVContextWindows`, `LTXVLoopingSampler`, `LTXVLatentUpsampler`, `LTXVAudioVAEDecode`, `LTXVEmptyLatentAudio` |
| ComfyUI-H3-Motion-Context | [github.com/NikoDemon80/ComfyUI-H3-Motion-Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context) | GPL-3.0 | `MiniMaxH3MotionContext`, `MiniMaxH3MotionContextTrim` — `engines/minimax_h3.py`'s `continue` mode only; `MiniMaxH3ImageToVideo`/`MiniMaxH3ReferenceToVideo` themselves are core |
| ComfyUI-MiniMax-Music-Production-Toolkit | [github.com/jplenio/ComfyUI-MiniMax-Music-Production-Toolkit](https://github.com/jplenio/ComfyUI-MiniMax-Music-Production-Toolkit) | MIT | `MiniMaxParametricEQ`, `MiniMaxAutoEQAnalyze`, `MiniMaxMasteringCompressor` — `engines/audio.py`'s mastering step (off by default) |

## Licence gates (restated, per AGENTS.md's "Rules for the agent")

- **Non-commercial**: Qwen-Image 2.1 (Qwen Research License), YuE2-3B and SheetSage2 (both
  CC BY-NC 4.0).
- **Revenue threshold, paid licence required above it**: LTX-2.5 — $10M/year annual revenue
  (LTX-2.x Community License §2.1); Stable Audio Open — $1M/year annual revenue (Stability AI
  Community License).
- **Revenue threshold, authorization required above it, plus territory**: MiniMax-H3 — open
  outside the EU/UK/South Korea/USA (elsewhere, apply first); everywhere, $20M/year commercial
  revenue requires separate written authorization from MiniMax (§IV.1); commercial UIs must
  display "MiniMax H3" (§IV.2).
- **Attribution required**: Stable Audio Open ("Powered by Stability AI" must be visible).
- **Display requirement**: MiniMax-Music3 — must prominently display "MiniMax-Music3" on the UI
  of any commercial product or service built on it (its LICENSE file's §3.1; the Hub's own
  `apache-2.0` metadata tag on the ComfyUI-packaged repo does not reflect this — read the actual
  LICENSE, not the tag); $20M/year aggregate revenue requires separate written authorization
  (§3.2).
- **No stated licence — ask first**: the 8-step MiniMax-H3 speed-pack alternative
  (`TenStrip/MinimaxH3-Turbo_Shenanigans`) — its author states no licence for the LoRA file
  itself; the recommended default is the 4-step LoRA above instead.
- **Program vs. output**: Blender is GPL-2.0-or-later, covering the program only — not what you
  render with it.
