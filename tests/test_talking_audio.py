"""Talking Head: the optional "Your own recording" field (opt-in; empty means today's typed line).

With no recording the mode builds exactly the graph it always has. With one, the recording is frozen in as the sound
(LoadAudio -> TrimAudioDuration to the clip's length -> AudioVAE encode -> zero noise mask), the typed line is not used
and nothing is quoted in the prompt, and the line-size guard stops applying to the typed line.

Run: python3 tests/test_talking_audio.py
"""
import json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
G = os.path.join(HERE, "golden")

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

import engines
import engines.ltx as ltx

m = json.load(open(os.path.join(G, "ltx_models.json")))
base = {"face": "me.png", "line": "Welcome to the forge.", "length": 97, "fps": 24, "seed": 7, "width": 1024, "height": 576}

print("the field")
f = {x["id"]: x for x in engines.fields("talking", "talking")} if False else {x["id"]: x for x in engines.fields("video", "talking")}
check("audio_slice is declared on the talking mode as an audio upload", f.get("audio_slice", {}).get("type") == "audio", list(f))
check("it is optional: no default, and the typed line field is still there", "default" not in f.get("audio_slice", {}) and "line" in f)

print("\nclassic (no recording): unchanged")
g0 = engines.graph_for("video", "talking", dict(base), m)
check("no LoadAudio / TrimAudioDuration node", "la" not in g0 and "lt" not in g0)
check("the empty audio latent is used", g0.get("377", {}).get("inputs", {}).get("audio_latent") == ["366", 0])
texts = [n["inputs"].get("text") for n in g0.values() if isinstance(n.get("inputs"), dict) and isinstance(n["inputs"].get("text"), str)]
check("the prompt quotes the typed line", any('"Welcome to the forge."' in t for t in texts), texts[:2])
g0b = engines.graph_for("video", "talking", dict(base, audio_slice=""), m)
check("an empty recording field is the same as none", json.dumps(g0, sort_keys=True) == json.dumps(g0b, sort_keys=True))

print("\nwith a recording")
g1 = engines.graph_for("video", "talking", dict(base, audio_slice="my_voice.wav", length=193), m)
check("LoadAudio reads the recording", g1.get("la", {}).get("class_type") == "LoadAudio" and g1["la"]["inputs"]["audio"] == "my_voice.wav")
lt = g1.get("lt", {})
check("TrimAudioDuration keeps exactly the clip's length (193 frames / 24 fps)",
      lt.get("class_type") == "TrimAudioDuration" and lt["inputs"]["start_index"] == 0.0 and abs(lt["inputs"]["duration"] - 193 / 24.0) < 1e-9, lt)
check("the encoder reads the trimmed audio", g1["ae"]["inputs"]["audio"] == ["lt", 0] and g1["ae"]["class_type"] == "LTXVAudioVAEEncode")
check("the audio latent is frozen with a zero mask and replaces the empty one",
      g1["snm"]["class_type"] == "SetLatentNoiseMask" and g1["sm"]["inputs"]["value"] == 0.0
      and g1["377"]["inputs"]["audio_latent"] == ["snm", 0] and "366" not in g1)
texts1 = [n["inputs"].get("text") for n in g1.values() if isinstance(n.get("inputs"), dict) and isinstance(n["inputs"].get("text"), str)]
check("the typed line is not in any prompt", not any("Welcome to the forge" in t for t in texts1), texts1[:2])
check("the prompt asks for a mouth in sync with the audio and quotes nothing",
      any("in sync with the audio" in t and '"' not in t for t in texts1), texts1[:2])
check("a shot note still rides along", any("Shot: warm light." in t for t in
      [n["inputs"].get("text") for n in engines.graph_for("video", "talking", dict(base, audio_slice="v.wav", look="warm light"), m).values()
       if isinstance(n.get("inputs"), dict) and isinstance(n["inputs"].get("text"), str)]))

print("\nthe plain video mode is untouched by the trim option")
gl = engines.graph_for("video", "ltx", dict(prompt="x", start_image="a.png", audio_slice="s.wav"), m)
check("ltx mode with audio_slice has no trim node (its slices are cut exactly elsewhere)", "lt" not in gl and gl["ae"]["inputs"]["audio"] == ["la", 0])

print("\nthe line-size guard ignores the typed line when a recording is given")
long_line = " ".join(["word"] * 200)
problems_line = ltx.talking_check({"line": long_line, "length": 97, "fps": 24}, {})
problems_rec = ltx.talking_check({"line": long_line, "length": 97, "fps": 24, "audio_slice": "v.wav"}, {})
check("a 200-word line on a 97-frame clip is flagged without a recording", any("words" in p for p in problems_line), problems_line)
check("the same line is not flagged when a recording replaces it", not any("words" in p for p in problems_rec), problems_rec)
check("the writer's length derivation sizes nothing from the line when a recording is given",
      ltx.talking_derive({"line": long_line, "audio_slice": "v.wav"}, {}) == {} and ltx.talking_derive({"line": long_line}, {}) != {})

print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
