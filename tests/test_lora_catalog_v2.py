"""LORA-2B acceptance gate: multi-family catalog, README-derived description/
trigger/strength/preview, per-family download folder, and family-based
download refusals (unknown family / traversal). RED reproduces the exact
pre-slice absence against 4df0f2b's own server.py (loaded via `git show`,
never a copy on disk); GREEN runs the same checks against the real, current
module.

Run: python3 tests/test_lora_catalog_v2.py
"""
import glob, importlib.util, json, os, subprocess, sys, tempfile, time
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
FIXTURES = os.path.join(HERE, "fixtures")

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below


def _raises_valueerror(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


def load_module_from_source(tag, source_path):
    spec = importlib.util.spec_from_file_location("srv_%s" % tag, source_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


srv = load_module_from_source("v2fixed", os.path.join(ROOT, "server.py"))

base_src = subprocess.run(["git", "show", "4df0f2b:server.py"], cwd=ROOT, capture_output=True,
                          text=True, check=True).stdout
red_dir = tempfile.mkdtemp(prefix="bwf_lora2_red_")
red_path = os.path.join(red_dir, "server_4df0f2b.py")
open(red_path, "w").write(base_src)
red = load_module_from_source("v2red", red_path)

print("RED (4df0f2b): none of LORA-2B's multi-family/description machinery exists yet")
check("RED: no style_families()", not hasattr(red, "style_families"))
check("RED: no _first_prose_sentence()", not hasattr(red, "_first_prose_sentence"))
check("RED: no _families_for_lane()", not hasattr(red, "_families_for_lane"))
check("RED: _lora_download_target takes the OLD (lane, repo, filename) shape, no family arg",
      red._lora_download_target.__code__.co_argcount == 3)
check("RED: catalog entries carry no \"name\"/\"description\" field (old _build_catalog shape)",
      "name" not in red._build_catalog.__code__.co_names
      or "description" not in red._build_catalog.__code__.co_names)

print()
print("GREEN (this branch): the real module has it all")
check("GREEN: style_families() exists", hasattr(srv, "style_families"))
check("GREEN: _families_for_lane() exists", hasattr(srv, "_families_for_lane"))
check("GREEN: _lora_download_target takes (lane, family_id, repo, filename)",
      srv._lora_download_target.__code__.co_argcount == 4)


def readme(name):
    return open(os.path.join(FIXTURES, "lora_readme_%s.md" % name), encoding="utf-8").read()


print()
print("Description extraction -- recorded real (trimmed) + synthetic README fixtures")
d1 = srv._first_prose_sentence(readme("prose_after_images"))
check("headings/images skipped; a very long first paragraph is cut at a word boundary with an ellipsis",
      d1 is not None and len(d1) <= 160 and d1.endswith("…")
      and d1.startswith("Qwen-Image-2.1-Natural-Exposure-LoRA is a LoRA adapter"), d1)

d2 = srv._first_prose_sentence(readme("html_badges_strength"))
check("heavy HTML/badge noise skipped; a hard-wrapped bold paragraph is joined before sentence-splitting",
      d2 == "Pruna-Qwen-Image-2.1 is a set of LoRA adapters that let Qwen/Qwen-Image-2.1 generate "
            "and edit images in only 5 or 8 steps.", d2)

d3 = srv._first_prose_sentence(readme("link_fallback"))
check("a heading-only/HTML-only card still finds its one real prose line (a link with trailing text)",
      d3 == "Download them in the Files & versions tab.", d3)

d4 = srv._first_prose_sentence(readme("no_prose"))
check("a card with NO prose line at all -> None (server falls back to \"No description...\")",
      d4 is None, d4)

d5 = srv._first_prose_sentence(readme("absolute_preview"))
check("a short, ordinary first paragraph is returned as-is",
      d5 == "A calm studio-lit portrait style, tuned for soft skin tones and warm rim light.", d5)

def _old_first_prose_sentence(readme_text):
    """The PRE-FIX _first_prose_sentence (verbatim: no trigger-line skip) --
    used ONLY here to demonstrate RED."""
    body, _fm = srv._strip_front_matter(readme_text)
    paragraph = []
    for raw_line in body.splitlines() + [""]:
        line = raw_line.strip()
        if not line or (line[:1] in (">", "|")) or srv._is_noise_line(line):
            if paragraph:
                text = srv._strip_markdown_inline(" ".join(paragraph))
                paragraph = []
                if text:
                    return srv._sentence_from(text)
            continue
        paragraph.append(line)
    return None


red_d6 = _old_first_prose_sentence(readme("trigger_before_description"))
check("RED: the pre-fix extractor returned the 'Trigger word:' line itself as the description "
      "(the live H3 Realism defect)", red_d6 == "Trigger word: r34l1sm", red_d6)

d6 = srv._first_prose_sentence(readme("trigger_before_description"))
check("GREEN: live-rig defect (2026-09-28) -- a 'Trigger word:' line is skipped, not returned as "
      "the description; the real next prose sentence is",
      d6 == "A photorealistic portrait style LoRA for MiniMax-H3, tuned for skin texture and studio light.", d6)

print()
print("Trigger words / recommended strength -- 'look for \"trigger word(s)\"/\"activation\" lines'")
check("a backtick-quoted trigger word wins over the surrounding sentence",
      srv._extract_trigger_words(readme("widget_preview")) == "Super Realism")
check("no trigger-word/activation line -> None",
      srv._extract_trigger_words(readme("prose_after_images")) is None)
check("LORA-2E #3: a 'Trigger Prompt:' line (no backticks) is recognised",
      srv._extract_trigger_words(readme("trigger_prompt")) == "cyberpunk neon portrait")
check("LORA-2E #3: the front-matter instance_prompt: key wins outright, no body scan needed",
      srv._extract_trigger_words("---\ninstance_prompt: sks_style_token\n---\n\nSome prose here.\n")
      == "sks_style_token")
check("LORA-2E #3: instance_prompt: null in front matter falls through to a body trigger line",
      srv._extract_trigger_words("---\ninstance_prompt: null\n---\n\nActivation token: fallback token\n")
      == "fallback token")
check("LORA-2E #3: a bare 'Trigger:' label (no \"word\") is recognised",
      srv._extract_trigger_words("Some intro text.\n\nTrigger: neon, chrome, rain\n") == "neon, chrome, rain")
check("a plain 'Recommended strength: 0.8' line",
      srv._extract_strength(readme("absolute_preview")) == 0.8)
check("'Keep the LoRA strength at 1.0.' (a real card's own phrasing)",
      srv._extract_strength(readme("html_badges_strength")) == 1.0)
check("no strength mentioned -> None", srv._extract_strength(readme("widget_preview")) is None)

print()
print("Preview image -- ONLY a front-matter widget/output/url, hosted on huggingface.co")
check("an absolute huggingface.co widget URL is kept",
      srv._extract_preview_image(readme("absolute_preview"), "owner/pack")
      == "https://huggingface.co/owner/pack/resolve/main/images/hero.png")
check("a relative widget URL resolves against the REAL huggingface.co (never the test HF_WEB override)",
      srv._extract_preview_image(readme("widget_preview"), "owner/pack")
      == "https://huggingface.co/owner/pack/resolve/main/images/3.png")
check("no widget at all in the front matter -> None", srv._extract_preview_image(readme("no_prose"), "o/p") is None)
check("no front matter at all -> None (never an exception)",
      srv._extract_preview_image("# just a heading\n\nsome text.", "o/p") is None)

print()
print("Multi-family catalog: fixture families (image/video/audio), NSFW kept + badged, files grouped")
import engines  # noqa: E402
_real_style_catalogs = engines.style_catalogs
FIXTURE_FAMILIES = [
    {"id": "qwen_image", "label": "Qwen Image", "cap": "image", "modes": ["t2i"], "role": "qwen_unet",
     "match": {"any": ["qwen_image", "qwen-image"]}, "hf_base": "Qwen/Qwen-Image-2.1", "folder": "qwen_image"},
    {"id": "ltx25", "label": "LTX-2.5", "cap": "video", "modes": ["t2v"], "role": "ltx_unet",
     "match": {"any": ["ltx"]}, "hf_base": "Lightricks/LTX-2.5", "folder": "ltx25"},
    {"id": "ace_step15", "label": "ACE-Step 1.5", "cap": "audio", "modes": ["music"], "role": "ace_unet",
     "match": {"any": ["ace_step", "ace-step"]}, "hf_base": "ACE-Step/ACE-Step-v1.5", "folder": "ace_step15"},
]
engines.style_catalogs = lambda: FIXTURE_FAMILIES
try:
    fams = srv.style_families()
    check("style_families() passes through a v2-shaped list unchanged", fams == FIXTURE_FAMILIES)

    m = {"qwen_unet": "qwen_image_fp8.safetensors", "ltx_unet": "ltx_2.5_q8.gguf", "ace_unet": "ace_step_1.5.safetensors"}
    present = srv._families_for_lane(m)
    check("all three fixture families (image/video/audio) are present for a lane with all three models",
          [f["id"] for f in present] == ["qwen_image", "ltx25", "ace_step15"], present)

    srv.CATALOG_CACHE["Qwen/Qwen-Image-2.1"] = (time.time(), [
        {"id": "owner/sfw-pack", "name": "Sfw Pack", "author": "owner", "downloads": 5, "likes": 1,
         "licence": "mit", "nsfw": False, "description": "d", "trigger_words": None, "strength": None,
         "preview": None, "files": [{"filename": "a.safetensors", "size": 10}]},
        {"id": "owner/nsfw-pack", "name": "Nsfw Pack", "author": "owner", "downloads": 2, "likes": 0,
         "licence": None, "nsfw": True, "description": "d", "trigger_words": None, "strength": None,
         "preview": None, "files": [{"filename": "b.safetensors", "size": 20}]},
    ])
    lane = {"id": "lv2", "name": "Lane v2", "caps": ["image"]}
    srv.LANE_BY_ID["lv2"] = lane
    with srv.DISCOVERY_LOCK:
        srv.DISCOVERY["lv2"] = {"models": m, "pools": {}, "checked": 1.0, "err": ""}
    payload, code = srv.Handler.api_catalog_loras(srv.Handler.__new__(srv.Handler),
                                                   {"lane": ["lv2"], "family": ["qwen_image"]})
    check("200 OK", code == 200, (payload, code))
    check("both packs listed -- owner ruling 2026-09-28: NSFW is never hidden, not even without a query flag",
          {c["id"] for c in payload["loras"]} == {"owner/sfw-pack", "owner/nsfw-pack"}, payload)
    check("the NSFW pack still carries nsfw:true so the page can badge it",
          next(c for c in payload["loras"] if c["id"] == "owner/nsfw-pack")["nsfw"] is True)
    check("files stay grouped per repo (a list of {filename,size} per pack)",
          all(isinstance(c["files"], list) and c["files"] for c in payload["loras"]), payload)
    check("families metadata lists all three fixture families",
          {f["id"] for f in payload["families"]} == {"qwen_image", "ltx25", "ace_step15"}, payload["families"])
    check("cap=image narrows the picked/selected family to qwen_image", payload["family"] == "qwen_image")

    payload_v, _ = srv.Handler.api_catalog_loras(srv.Handler.__new__(srv.Handler),
                                                  {"lane": ["lv2"], "cap": ["video"]})
    check("cap=video narrows to the video family only (ltx25), not qwen_image",
          [f["id"] for f in payload_v["families"]] == ["ltx25"], payload_v["families"])
finally:
    engines.style_catalogs = _real_style_catalogs

print()
print("Download lands in <family folder>/; refused for unknown family / traversal / file not in that family")
import tempfile as _tf  # noqa: E402


def _dl_lane(loras_dir):
    lane = {"id": "dlv2", "name": "DL Lane", "caps": ["image"], "downloads": {"loras_dir": loras_dir}}
    srv.LANE_BY_ID["dlv2"] = lane
    with srv.DISCOVERY_LOCK:
        srv.DISCOVERY["dlv2"] = {"models": {"qwen_unet": "qwen_image_fp8.safetensors"}, "pools": {}, "checked": 1.0, "err": ""}
    return lane


engines.style_catalogs = lambda: [FIXTURE_FAMILIES[0]]   # only qwen_image, folder "qwen_image"
try:
    srv.CATALOG_CACHE["Qwen/Qwen-Image-2.1"] = (time.time(), [
        {"id": "owner/pack", "name": "Pack", "author": "owner", "downloads": 1, "likes": 0, "licence": "mit",
         "nsfw": False, "description": "d", "trigger_words": None, "strength": None, "preview": None,
         "files": [{"filename": "s.safetensors", "size": 10}]}])

    lane = _dl_lane(_tf.mkdtemp(prefix="bwf_lora2_dl_"))
    url, dest, maxb = srv._lora_download_target(lane, "qwen_image", "owner/pack", "s.safetensors")
    check("the download lands under <loras_dir>/<family folder>/<file>",
          dest.endswith(os.path.join("qwen_image", "s.safetensors")), dest)

    check("a folder id not declared in style_catalogs is refused",
          _raises_valueerror(lambda: srv._lora_download_target(lane, "not-a-real-family",
                                                                "owner/pack", "s.safetensors")))

    evil = dict(FIXTURE_FAMILIES[0], id="evil", folder="../../etc")
    engines.style_catalogs = lambda: [evil]
    check("traversal via the family's own folder is refused",
          _raises_valueerror(lambda: srv._lora_download_target(lane, "evil", "owner/pack", "s.safetensors")))
    engines.style_catalogs = lambda: [FIXTURE_FAMILIES[0]]

    check("a file not listed for that repo in that family's catalog is still refused",
          _raises_valueerror(lambda: srv._lora_download_target(lane, "qwen_image", "owner/pack", "nope.safetensors")))

    # LORA-2E #2: a family DECLARED in style_catalogs (ltx25) but whose
    # model is NOT discovered on THIS lane -- tightened past LORA-2B's
    # "declared anywhere" check to "present on this lane" (_families_for_lane).
    engines.style_catalogs = lambda: FIXTURE_FAMILIES[:2]   # qwen_image + ltx25
    check("a family declared in style_catalogs but not present on this lane is refused, not just any declared id",
          _raises_valueerror(lambda: srv._lora_download_target(lane, "ltx25", "owner/pack", "s.safetensors")))
    engines.style_catalogs = lambda: [FIXTURE_FAMILIES[0]]

    url2, dest2, _ = srv._lora_download_target(lane, None, "owner/pack", "s.safetensors")
    check("family omitted + exactly one family present -> defaults to it (pre-LORA-2B call shape kept working)",
          dest2 == dest, (dest, dest2))

    engines.style_catalogs = lambda: FIXTURE_FAMILIES[:2]   # qwen_image + ltx25, both present below
    with srv.DISCOVERY_LOCK:
        srv.DISCOVERY["dlv2"]["models"]["ltx_unet"] = "ltx_2.5_q8.gguf"
    check("family omitted + more than one family present -> refused, not a silent guess",
          _raises_valueerror(lambda: srv._lora_download_target(lane, None, "owner/pack", "s.safetensors")))
finally:
    engines.style_catalogs = _real_style_catalogs


print()
print("Live-rig defect (2026-09-28): family PRESENCE must use the match rule's POSITIVE terms only, "
      "never its \"none\" exclusion list, against REAL engine packs + REAL lane role filenames")
# Taken verbatim from the live rig lane (`curl -s http://<rig>:3998/api/lanes`, "rig" -> "files"),
# 2026-09-28 -- every one of the 6 families' own base-model filename, including the two that
# tripped the bug: ltx_transformer names a DISTILLED base checkpoint ("distill" in the filename)
# and ace_unet names a TURBO base checkpoint ("turbo" in the filename); both are excluded by their
# own family's "none" list, which exists to keep speed/distillation LoRA ADD-ONS out of the
# "Browse styles" catalog -- never meant to apply to the base model itself.
REAL_RIG_FILES = {
    "ace_unet": "ace_step_1.5/acestep_v1.5_turbo.safetensors",
    "music3_unet": "minimax_music3/minimax_music3_dit_int8_convrot.safetensors",
    "yue2_ckpt": "yue2_3b_int8_convrot.safetensors",
    "ltx_transformer": "ltx-2.5-22b-distilled-transformer-nvfp4.safetensors",
    "h3_unet_fl2va": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "qwen_unet": "qwen_image_2.1_nvfp4.safetensors",
}
EXPECTED_REAL_FAMILY_IDS = {"ace_step15", "music3", "yue2", "ltx25", "minimax_h3", "qwen_image"}


def _old_families_for_lane(m):
    """The PRE-FIX _families_for_lane (verbatim: applies the WHOLE match
    rule, "none" included) -- used ONLY here to demonstrate RED."""
    out = []
    for sc in engines.style_catalogs():
        name = (m.get(sc["role"]) or "").lower()
        if name and srv._rule_matches([name], sc.get("match") or {}):
            out.append(sc)
    return out


red_present = {sc["id"] for sc in _old_families_for_lane(REAL_RIG_FILES)}
check("RED: the pre-fix presence check drops ltx25 and ace_step15 (real distilled/turbo base models)",
      red_present == EXPECTED_REAL_FAMILY_IDS - {"ltx25", "ace_step15"}, red_present)

green_present = {sc["id"] for sc in srv._families_for_lane(REAL_RIG_FILES)}
check("GREEN: the real _families_for_lane finds all six real families present on the real rig lane",
      green_present == EXPECTED_REAL_FAMILY_IDS, green_present)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
