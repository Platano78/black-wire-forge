---
tags:
- text-to-image
- lora
- diffusers
- template:diffusion-lora
widget:
- text: >-
    Super Realism, Woman in a red jacket, snowy, in the style of hyper-realistic
    portraiture, caninecore, mountainous vistas, timeless beauty, palewave,
    iconic, distinctive noses --ar 72:101 --stylize 750 --v 6
  output:
    url: images/3.png
- text: >-
    Super Realism, Headshot of handsome young man, wearing dark gray sweater
    with buttons and big shawl collar, brown hair and short beard.
  output:
    url: images/2.png
base_model: black-forest-labs/FLUX.1-dev
instance_prompt: Super Realism
---
![strangerzonehf/Flux-Super-Realism-LoRA](images/sz.png)

<Gallery />

## Model description for super realism engine

Image Processing Parameters

| Parameter                 | Value  | Parameter                 | Value  |
|---------------------------|--------|---------------------------|--------|
| LR Scheduler              | constant | Noise Offset              | 0.03   |

## Comparison between the base model and related models.

Comparison between the base model FLUX.1-dev and its adapter, a LoRA model tuned for super-realistic realism.

## Trigger words

> [!WARNING]
> **Trigger words:**  You should use `Super Realism` to trigger the image generation.

- The trigger word is not mandatory; ensure that words like "realistic" and "realism" appear in the image description.

## Download model

Weights for this model are available in Safetensors format.
