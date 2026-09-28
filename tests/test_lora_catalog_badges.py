"""Acceptance gate for UX-2 #10 (workflow-only badge) and #11 (Installed
badge), against the REAL api_catalog_loras -- a small in-memory catalog
(CATALOG_CACHE set directly, no HF fetch) so the two badges are exercised
against known repo/file names and a known "lora" pool, deterministically.

Run: python3 tests/test_lora_catalog_badges.py
"""
import importlib.util
import os
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402,F401 -- must run before server.py's own exec_module below
spec = importlib.util.spec_from_file_location("srv_lora_badges", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

HF_BASE = "Test/Base-Model"
# One real style, one turbo-speed LoRA (the family's OWN "none" word), one
# IC-LoRA (also a "none" word) -- the exact "packs next to real styles"
# shape the audit described.
CATALOG = [
    {"id": "someone/nice-anime-style", "name": "Nice Anime Style", "author": "someone", "downloads": 100,
     "likes": 5, "licence": "mit", "nsfw": False,
     "files": [{"filename": "nice_anime_style.safetensors", "size": 12345}],
     "description": "a real style", "trigger_words": None, "strength": None, "preview": None},
    {"id": "someone/turbo-speed-lora", "name": "Turbo Speed LoRA", "author": "someone", "downloads": 50,
     "likes": 1, "licence": "mit", "nsfw": False,
     "files": [{"filename": "turbo_speed_v1.safetensors", "size": 999}],
     "description": "a speed-distillation pack", "trigger_words": None, "strength": None, "preview": None},
    {"id": "someone/ic-lora-pack", "name": "IC-LoRA Pack", "author": "someone", "downloads": 20,
     "likes": 0, "licence": None, "nsfw": False,
     "files": [{"filename": "ic_lora_edit.safetensors", "size": 4321}],
     "description": "an IC-LoRA workflow pack", "trigger_words": None, "strength": None, "preview": None},
]
srv.CATALOG_CACHE[HF_BASE] = (time.time(), CATALOG)

FAMILY = {"id": "testfam", "label": "Test Family", "cap": "image", "modes": ["t2i"], "role": "test_unet",
          "match": {"any": ["test-base"], "none": ["turbo", "ic-lora", "ic_lora"]},
          "hf_base": HF_BASE, "folder": "test_family"}
srv._TEST_STYLE_FAMILIES = __import__("json").dumps([FAMILY])

LANE = {"id": "badge-lane", "name": "Badge lane", "caps": ["image"]}
srv.LANE_BY_ID[LANE["id"]] = LANE
with srv.DISCOVERY_LOCK:
    # nice_anime_style.safetensors already on this lane's disk (case
    # variation on purpose -- the match must be case-insensitive); the
    # turbo/ic-lora files are NOT installed.
    srv.DISCOVERY[LANE["id"]] = {"models": {"test_unet": "test-base_v1.safetensors"},
                                  "pools": {"lora": ["Nice_Anime_Style.safetensors", "some_other_style.safetensors"]},
                                  "checked": 1.0, "err": ""}

obj = srv.Handler.__new__(srv.Handler)
payload, code = srv.Handler.api_catalog_loras(obj, {"lane": [LANE["id"]]})
check("200 OK, one family, three entries (nothing hidden -- owner ruling: everything lives together)",
      code == 200 and payload["family"] == "testfam" and len(payload["loras"]) == 3, payload)

by_id = {e["id"]: e for e in payload["loras"]}

print("UX-2 #10: workflow-only badge")
check("a real style: workflow_only is False", by_id["someone/nice-anime-style"]["workflow_only"] is False)
check("a turbo/speed pack matching the family's OWN \"none\" word: workflow_only is True",
      by_id["someone/turbo-speed-lora"]["workflow_only"] is True)
check("an IC-LoRA pack matching \"none\": workflow_only is True",
      by_id["someone/ic-lora-pack"]["workflow_only"] is True)

print()
print("UX-2 #11: Installed badge, matched by filename (case-insensitive), no size compare available")
style_files = by_id["someone/nice-anime-style"]["files"]
check("the already-on-disk file is marked installed",
      style_files[0]["installed"] is True, style_files)
turbo_files = by_id["someone/turbo-speed-lora"]["files"]
check("a file NOT on this lane's disk is not marked installed",
      turbo_files[0]["installed"] is False, turbo_files)

print()
print("a family with no \"none\" words at all never badges anything workflow-only")
FAMILY_NO_NONE = dict(FAMILY, id="nonone", match={"any": ["test-base"]})
srv._TEST_STYLE_FAMILIES = __import__("json").dumps([FAMILY_NO_NONE])
payload2, _ = srv.Handler.api_catalog_loras(obj, {"lane": [LANE["id"]]})
check("nothing is workflow_only when the family declares no \"none\" list",
      all(e["workflow_only"] is False for e in payload2["loras"]), payload2["loras"])

print()
print("UX-2 #11 regression: pool entries carry a subfolder (real ComfyUI shape) -- basename match")
HF_BASE3 = "Test/Base-Model-3"
CATALOG3 = [
    {"id": "someone/vh5tape", "name": "VH5 Tape", "author": "someone", "downloads": 10,
     "likes": 0, "licence": "mit", "nsfw": False,
     "files": [{"filename": "vh5tape-comfyui.safetensors", "size": 111}],
     "description": "a style whose pool entry carries a '/' subfolder", "trigger_words": None,
     "strength": None, "preview": None},
    {"id": "someone/ypack", "name": "Y Pack", "author": "someone", "downloads": 5,
     "likes": 0, "licence": "mit", "nsfw": False,
     "files": [{"filename": "y.safetensors", "size": 222}],
     "description": "a style whose pool entry carries a Windows '\\' subfolder", "trigger_words": None,
     "strength": None, "preview": None},
]
srv.CATALOG_CACHE[HF_BASE3] = (time.time(), CATALOG3)

FAMILY3 = {"id": "testfam3", "label": "Test Family 3", "cap": "image", "modes": ["t2i"], "role": "test_unet3",
           "match": {"any": ["test-base3"], "none": []},
           "hf_base": HF_BASE3, "folder": "test_family3"}
srv._TEST_STYLE_FAMILIES = __import__("json").dumps([FAMILY3])

LANE3 = {"id": "badge-lane-subfolder", "name": "Subfolder lane", "caps": ["image"]}
srv.LANE_BY_ID[LANE3["id"]] = LANE3
with srv.DISCOVERY_LOCK:
    # Real live-rig shape (owner-verified 2026-09-28): ComfyUI's lora pool
    # lists names WITH their subfolder ('minimax_h3/x.safetensors',
    # 'library/minimax_h3/x.safetensors'), and on Windows hosts possibly
    # with backslashes -- catalog filenames stay bare, so matching must be
    # on the pool entry's BASENAME, not the whole string.
    srv.DISCOVERY[LANE3["id"]] = {
        "models": {"test_unet3": "test-base3_v1.safetensors"},
        "pools": {"lora": [
            "minimax_h3/vh5tape-comfyui.safetensors",
            "library/minimax_h3/vh5tape-comfyui.safetensors",
            "a\\b\\y.safetensors",
        ]},
        "checked": 1.0, "err": "",
    }

payload3, code3 = srv.Handler.api_catalog_loras(obj, {"lane": [LANE3["id"]]})
check("200 OK for the subfolder-pool lane", code3 == 200 and payload3["family"] == "testfam3", payload3)
by_id3 = {e["id"]: e for e in payload3["loras"]}
check("a file whose pool entry carries a '/' subfolder is still marked installed",
      by_id3["someone/vh5tape"]["files"][0]["installed"] is True, by_id3["someone/vh5tape"]["files"])
check("a file whose pool entry carries a Windows '\\' subfolder is still marked installed",
      by_id3["someone/ypack"]["files"][0]["installed"] is True, by_id3["someone/ypack"]["files"])

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
