---
license: other
license_name: qwen-research
base_model: Qwen/Qwen-Image-2.1
library_name: diffusers
pipeline_tag: text-to-image
tags:
  - qwen
  - image-generation
  - lora
  - distilled
---
<!-- header start -->
<p align="center">
  <img src="assets/Pruna-Qwen-Image-2.1.png" width="800">
</p>
<!-- header end -->

<p align="center">
  <a href="https://github.com/PrunaAI/pruna">
    <img src="https://img.shields.io/badge/GitHub-PrunaAI-9334E9?style=plastic&logo=github&logoColor=white" alt="GitHub">
  </a>
</p>
<div align="center">

<h1 style="color: #9334E9;">Pruna-Qwen-Image-2.1</h1>

<h2>Few-step LoRA adapters for Qwen-Image-2.1</h2>

</div>

**Pruna-Qwen-Image-2.1 is a set of LoRA adapters that let
[`Qwen/Qwen-Image-2.1`](https://huggingface.co/Qwen/Qwen-Image-2.1) generate
and edit images in only 5 or 8 steps. The adapters load on top of the base
model, so the pipeline, text encoder, and VAE stay unchanged.**

> **v0.1: first release, work in progress.** Pruna-Qwen-Image-2.1 does not yet match the
> visual quality of the base model.

### Recommended settings

- **No CFG.** Keep `true_cfg_scale=1.0` and do not pass a negative prompt.
- **Keep the LoRA strength at 1.0.**
- **Write detailed prompts for text-to-image.**

## Limitations

- This is a first version. Quality is below that of the base model.
