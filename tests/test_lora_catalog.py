"""LORA-1 Build B acceptance gate: the "Browse styles" catalog build --
filtering (NSFW hidden unless advanced, licence-unknown kept), the family ->
HF base-model map, and the 10-minute cache -- against a RECORDED Hugging
Face API fixture (tests/fixtures/hf_qwen_loras.json). NO live network call
happens in this file; http_get_json is monkeypatched.

The fixture was captured live 2026-09-28 against:
  GET https://huggingface.co/api/models?filter=base_model:adapter:Qwen/Qwen-Image-2.1&sort=downloads&limit=50
  GET https://huggingface.co/api/models/<id>?blobs=true   (per listed repo)
and trimmed to 3 representative repos: one with an MIT licence and safetensors
files, one with no license tag or cardData.license at all (kept as "licence
unknown"), and one carrying the Hub's "not-for-all-audiences" tag.

Run: python3 tests/test_lora_catalog.py
"""
import importlib.util, json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

spec = importlib.util.spec_from_file_location("srv_cat", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "hf_qwen_loras.json")))
CALLS = []


def fake_http_get_json(url, timeout=8.0):
    CALLS.append(url)
    if "?filter=" in url:
        return FIXTURE["listing"]
    for repo_id, detail in FIXTURE["details"].items():
        if "/api/models/%s" % repo_id in url:
            return detail
    raise AssertionError("unexpected URL: %s" % url)


srv.http_get_json = fake_http_get_json
srv.CATALOG_CACHE.clear()

print("_hf_base_for_lane: the qwen_image family maps to the verified HF base id")
check("qwen_unet filename -> Qwen/Qwen-Image-2.1",
      srv._hf_base_for_lane({"qwen_unet": "qwen_image_2.1_Q6_K.gguf"}) == "Qwen/Qwen-Image-2.1")
check("an unrelated/unknown family -> None", srv._hf_base_for_lane({"qwen_unet": "some_other_model.gguf"}) is None)
check("no qwen_unet at all -> None", srv._hf_base_for_lane({}) is None)

print()
print("_build_catalog / catalog_for: shape, licence and files per entry")
catalog = srv.catalog_for("Qwen/Qwen-Image-2.1")
check("one entry per fixture repo", len(catalog) == len(FIXTURE["listing"]), len(catalog))
mit = next(c for c in catalog if c["id"] == "Alissonerdx/BFS-Best-Face-Swap")
check("a repo with a real licence carries it", mit["licence"] == "mit", mit["licence"])
check("its .safetensors files carry real sizes", all(f["size"] for f in mit["files"]), mit["files"])
no_lic = next(c for c in catalog if c["id"] == "e-n-v-y/Qwen-Image-2.1-Fix")
check("a repo with no cardData.license -> licence None (\"licence unknown\" is the UI's job)",
      no_lic["licence"] is None, no_lic["licence"])
nsfw_entry = next((c for c in catalog if c["nsfw"]), None)
check("the not-for-all-audiences repo is flagged nsfw", nsfw_entry is not None, [c["id"] for c in catalog])

print()
print("filtering: /api/catalog/loras hides nsfw unless advanced=1")
obj = srv.Handler.__new__(srv.Handler)
LANE = {"id": "q", "name": "Qwen lane", "caps": ["image"]}
srv.LANE_BY_ID[LANE["id"]] = LANE
with srv.DISCOVERY_LOCK:
    srv.DISCOVERY[LANE["id"]] = {"models": {"qwen_unet": "qwen_image_2.1_Q6_K.gguf"},
                                 "pools": {}, "checked": 1.0, "err": ""}
plain, code = srv.Handler.api_catalog_loras(obj, {"lane": ["q"]})
check("advanced=0 (default): 200 and nsfw entries excluded",
      code == 200 and all(not c["nsfw"] for c in plain["loras"]), plain)
adv, code2 = srv.Handler.api_catalog_loras(obj, {"lane": ["q"], "advanced": ["1"]})
check("advanced=1: the nsfw entry is included", any(c["nsfw"] for c in adv["loras"]), adv)
check("licence-unknown entries are still shown (never dropped)",
      any(c["licence"] is None for c in adv["loras"]), adv)

print()
print("an unknown picture family returns an empty list with a plain sentence, no HF call for it")
UNKNOWN_LANE = {"id": "u", "name": "Unknown lane", "caps": ["image"]}
srv.LANE_BY_ID[UNKNOWN_LANE["id"]] = UNKNOWN_LANE
with srv.DISCOVERY_LOCK:
    srv.DISCOVERY[UNKNOWN_LANE["id"]] = {"models": {"qwen_unet": "some_other_model.gguf"},
                                         "pools": {}, "checked": 1.0, "err": ""}
before = len(CALLS)
empty, code3 = srv.Handler.api_catalog_loras(obj, {"lane": ["u"]})
check("empty loras list", empty["loras"] == [], empty)
check("a plain-sentence note is included", bool(empty.get("note")), empty)
check("no HF call was made for the unknown family", len(CALLS) == before)

print()
print("caching: a second call inside the 10-minute window makes no new HF calls")
before2 = len(CALLS)
srv.Handler.api_catalog_loras(obj, {"lane": ["q"]})
check("no new HF calls (cache hit)", len(CALLS) == before2, (before2, len(CALLS)))
srv.CATALOG_CACHE["Qwen/Qwen-Image-2.1"] = (0.0, srv.CATALOG_CACHE["Qwen/Qwen-Image-2.1"][1])  # force stale
before3 = len(CALLS)
srv.Handler.api_catalog_loras(obj, {"lane": ["q"]})
check("a stale cache entry DOES trigger a rebuild", len(CALLS) > before3, (before3, len(CALLS)))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
