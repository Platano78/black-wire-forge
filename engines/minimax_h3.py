"""Engine pack: MiniMax-H3 (first/last-to-video, reference-to-video, turbo).

Copied from server.py's pre-extraction ROLE_RULES/ROLE_POOL entries,
abilities()/missing_for() labels, describe_video(), and the h3_*_graph
builders. Logic is verbatim; only the lane->models indirection is replaced
by the contract's direct ``models`` dict.

Note on the text encoder: server.py requires h3_clip_nvfp4 OR h3_clip_int8,
but the contract's ``provides`` can only list roles that must ALL resolve,
so this pack lists h3_clip_nvfp4 (the role server.py's own missing_for
treats as "the H3 text encoder" in the golden rig). A rig with only the int8
clip would report fl2va/ref2v missing here, whereas server.py would not.
"""
import re

from . import unet_loader, quant_words


def _describe(models):
    q = quant_words(models.get("h3_unet_fl2va") or models.get("h3_unet_ref2va"))
    return "MiniMax-H3" + (" (%s)" % q if q else "")


def h3_fl2va_graph(p, m):
    """The 'cheers' recipe: fl2va unet + NVFP4 AWQ encoder + MiniMaxH3ImageToVideo,
    res_multistep/simple, 20 steps, no LoRA. first_frame/last_frame optional, and
    with neither wired it is pure text-to-video (which is what cheers was)."""
    clip = p.get("encoder") or m.get("h3_clip_nvfp4") or m["h3_clip_int8"]
    g = {
        "6": unet_loader(m["h3_unet_fl2va"]),
        "13": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "minimax", "device": "default"}},
        "11": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_video"]}},
        "24": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_audio"]}},
        "104": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["13", 0], "vae": ["11", 0], "prompt": p["prompt"],
            "width": p["width"], "height": p["height"], "length": p["length"]}},
        "16": {"class_type": "BasicGuider", "inputs": {"model": ["6", 0], "conditioning": ["104", 0]}},
        "17": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "BasicScheduler", "inputs": {
            "model": ["6", 0], "scheduler": "simple", "steps": p["steps"], "denoise": 1.0}},
        "15": {"class_type": "RandomNoise", "inputs": {"noise_seed": p["seed"]}},
        "14": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["15", 0], "guider": ["16", 0], "sampler": ["17", 0],
            "sigmas": ["9", 0], "latent_image": ["104", 1]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["11", 0]}},
        "23": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["14", 0], "vae": ["24", 0]}},
        "91": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "audio": ["23", 0], "fps": 24.0}},
        "92": {"class_type": "SaveVideo", "inputs": {
            "video": ["91", 0], "filename_prefix": "blackwire/VID", "format": "auto", "codec": "auto"}},
    }
    if p.get("turbo_lora"):
        # Model-only LoRA: the H3 CLIP is separate, so only the UNET gets patched.
        g["7"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["6", 0], "lora_name": m["h3_turbo_lora"], "strength_model": 1.0}}
        g["16"]["inputs"]["model"] = ["7", 0]
        g["9"]["inputs"]["model"] = ["7", 0]
    if p.get("first_frame"):
        g["200"] = {"class_type": "LoadImage", "inputs": {"image": p["first_frame"]}}
        g["104"]["inputs"]["first_frame"] = ["200", 0]
    if p.get("last_frame"):
        g["201"] = {"class_type": "LoadImage", "inputs": {"image": p["last_frame"]}}
        g["104"]["inputs"]["last_frame"] = ["201", 0]
    return g


def h3_continue_graph(p, m):
    """The 'continue' recipe: same fl2va unet + NVFP4 AWQ encoder +
    MiniMaxH3ImageToVideo as h3_fl2va_graph, but with NO starting picture --
    instead the previous shot's harvested clip (frames + audio) is fed
    through MiniMaxH3MotionContext (route C, L2's frames+audio arm --
    our internal l2-continuity-2026-09-23 measurement notes, shot2_C) so
    the new shot's motion and
    sound carry on from where the previous one left off, instead of
    freezing/restarting at the cut. MotionContextTrim then drops the pinned
    context frames both the base recipe and the base MiniMaxH3ImageToVideo
    latent are still built from (the trim, not this graph, is what delivers
    a shot 22 frames shorter than requested -- L2's own finding). Like
    fl2va's own optional first_frame/last_frame, `prev_video` is optional at
    THIS level -- server.py's cable resolver (resolve_slot_cables/K3) is
    what actually refuses a shot whose jack is plugged but unresolvable; a
    shot with no cable at all (e.g. the first of a chain) just renders
    without the Motion-Context wiring, plain text-to-video."""
    clip = p.get("encoder") or m.get("h3_clip_nvfp4") or m["h3_clip_int8"]
    g = {
        "6": unet_loader(m["h3_unet_fl2va"]),
        "13": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "minimax", "device": "default"}},
        "11": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_video"]}},
        "24": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_audio"]}},
        "104": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["13", 0], "vae": ["11", 0], "prompt": p["prompt"],
            "width": p["width"], "height": p["height"], "length": p["length"]}},
        "16": {"class_type": "BasicGuider", "inputs": {"model": ["6", 0], "conditioning": ["104", 0]}},
        "17": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "BasicScheduler", "inputs": {
            "model": ["6", 0], "scheduler": "simple", "steps": p["steps"], "denoise": 1.0}},
        "15": {"class_type": "RandomNoise", "inputs": {"noise_seed": p["seed"]}},
        "14": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["15", 0], "guider": ["16", 0], "sampler": ["17", 0],
            "sigmas": ["9", 0], "latent_image": ["104", 1]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["11", 0]}},
        "23": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["14", 0], "vae": ["24", 0]}},
        "91": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "audio": ["23", 0], "fps": 24.0}},
        "92": {"class_type": "SaveVideo", "inputs": {
            "video": ["91", 0], "filename_prefix": "blackwire/CONT", "format": "auto", "codec": "auto"}},
    }
    if p.get("turbo_lora"):
        g["7"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["6", 0], "lora_name": m["h3_turbo_lora"], "strength_model": 1.0}}
        g["16"]["inputs"]["model"] = ["7", 0]
        g["9"]["inputs"]["model"] = ["7", 0]
    if p.get("prev_video"):
        g["220"] = {"class_type": "LoadVideo", "inputs": {"file": p["prev_video"]}}
        g["221"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["220", 0]}}
        g["222"] = {"class_type": "MiniMaxH3MotionContext", "inputs": {
            "conditioning": ["104", 0], "vae": ["11", 0], "latent": ["104", 1],
            "context_length": "22", "audio_context_length": 24,
            "context_frames": ["221", 0], "context_audio": ["221", 1], "audio_vae": ["24", 0]}}
        g["16"]["inputs"]["conditioning"] = ["222", 0]
        g["223"] = {"class_type": "MiniMaxH3MotionContextTrim", "inputs": {
            "images": ["10", 0], "trim_frames": ["222", 1], "audio": ["23", 0],
            "fps": 24.0, "match_tail": True}}
        g["91"]["inputs"]["images"] = ["223", 0]
        g["91"]["inputs"]["audio"] = ["223", 1]
    return g


def h3_ref2va_graph(p, m):
    """ref2va: up to 9 reference images + up to 3 reference videos (frames at 24fps,
    2-15s each) with the first video's audio carried through. INT8 encoder by default.
    Audio is NEVER fed in as TTS -- H3 speaks the dialogue in the prompt itself."""
    clip = p.get("encoder") or m.get("h3_clip_int8") or m["h3_clip_nvfp4"]
    g = {
        "159": unet_loader(m["h3_unet_ref2va"]),
        "160": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "minimax", "device": "default"}},
        "162": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_video"]}},
        "163": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_audio"]}},
        "164": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["160", 0], "vae": ["162", 0], "audio_vae": ["163", 0], "prompt": p["prompt"],
            "width": p["width"], "height": p["height"], "length": p["length"],
            "ref_image_size": p.get("ref_image_size", "match")}},
        "166": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "167": {"class_type": "BasicScheduler", "inputs": {
            "model": ["159", 0], "scheduler": "simple", "steps": p["steps"], "denoise": 1.0}},
        "168": {"class_type": "BasicGuider", "inputs": {"model": ["159", 0], "conditioning": ["164", 0]}},
        "169": {"class_type": "RandomNoise", "inputs": {"noise_seed": p["seed"]}},
        "177": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["169", 0], "guider": ["168", 0], "sampler": ["166", 0],
            "sigmas": ["167", 0], "latent_image": ["164", 1]}},
        "180": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["177", 0], "vae": ["163", 0]}},
        "181": {"class_type": "VAEDecode", "inputs": {"samples": ["177", 0], "vae": ["162", 0]}},
        "182": {"class_type": "CreateVideo", "inputs": {"fps": 24, "bit_depth": 8,
                                                        "images": ["181", 0], "audio": ["180", 0]}},
        "184": {"class_type": "SaveVideo", "inputs": {
            "filename_prefix": "blackwire/REF", "format": "auto", "codec": "auto", "video": ["182", 0]}},
    }
    for i, name in enumerate((p.get("ref_images") or [])[:9]):
        nid = str(400 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g["164"]["inputs"]["ref_images.ref_image_%d" % i] = [nid, 0]
    for i, name in enumerate((p.get("ref_videos") or [])[:3]):
        vid, cid = str(300 + i * 2), str(301 + i * 2)
        g[vid] = {"class_type": "LoadVideo", "inputs": {"file": name}}
        g[cid] = {"class_type": "GetVideoComponents", "inputs": {"video": [vid, 0]}}
        g["164"]["inputs"]["ref_videos.ref_video_%d" % i] = [cid, 0]
        if i == 0 and p.get("keep_audio", True):
            g["164"]["inputs"]["ref_video_audios.ref_video_audio_0"] = [cid, 1]
    return g


# ── writers and a fixer (the Motion guide's skills; engines/__init__.py "writers"/"revisers") ──
# Rules restated from this pack's prompt_guides and docstrings above and
# guides/motion/knowledge.md (H3's named camera moves; continue = the SAME
# shot carrying on, measured as a hard cut otherwise).

H3_FPS = 24

# "(dog:1.3)", "((snow))", "[wind]": token weights and brackets the model reads as words.
_WEIGHT_RE = re.compile(r"\([^()]*:\s*\d+(?:\.\d+)?\s*\)|\(\([^()]*\)\)|\[[^\[\]]*\]")
# Words that start a new composition, which in continue mode overrides the
# ~0.9 s of carried motion (prompt_guides["continue"]). A plain list.
H3_NEW_SHOT_WORDS = (
    "cut to", "cuts to", "new angle", "different angle", "another angle", "reverse angle", "reverse shot",
    "new shot", "next shot", "new scene", "scene changes", "the scene shifts", "switch to", "switches to",
    "meanwhile", "we now see", "now we see", "from a new", "from a different", "from another",
    "a close-up of", "in close-up", "in a close-up", "is shown in a close-up", "zooms in on", "zoom in on",
)
_NEW_SHOT_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in H3_NEW_SHOT_WORDS) + r")\b", re.IGNORECASE)


# The engine's own task-type prefix, which opens a prompt in square brackets:
# one or more of the vendor's task words joined by " + " ("[reference
# generation] ...", "[video editing + reference generation + audio reuse] ...",
# "[video continuation + keyframe completion] ..."; the prompt spec published
# with MiniMax-H3, huggingface.co/MiniMaxAI/MiniMax-H3). A CLOSED vocabulary:
# any other bracket ("[wind]") is not a prefix. It is not a token weight, so
# the check reads past it; the same pattern is the pack's "task_prefix".
H3_TASK_WORDS = ("reference generation", "audio reference", "video editing", "audio reuse",
                 "video continuation", "keyframe completion")
_TASK = "(?:" + "|".join(re.escape(w) for w in H3_TASK_WORDS) + ")"
_TASK_PREFIX_RE = re.compile(r"^\s*\[" + _TASK + r"(?: \+ " + _TASK + r")*\]")


def h3_shot_check(values, request):
    """The fl2va / ref2v writer's check, also the Make-time guard: token
    weights or brackets in the prompt (past a leading task-type prefix)."""
    found = _WEIGHT_RE.findall(_TASK_PREFIX_RE.sub("", values.get("prompt") or "", count=1))
    if not found:
        return []
    return ["The prompt has token weights or brackets (%s): this model reads them as words. Write plain "
            "sentences instead." % ", ".join(found[:3])]


def h3_continue_check(values, request):
    """The continue writer's check, also the Make-time guard: a prompt that
    starts a new composition instead of carrying the same shot on."""
    out = h3_shot_check(values, request)
    found = []
    for m in _NEW_SHOT_RE.finditer(values.get("prompt") or ""):
        if m.group(1).lower() not in found:
            found.append(m.group(1).lower())
    if found:
        out.append("The prompt says %s: that starts a new shot, and a new shot overrides the motion carried "
                   "over from the shot before, so it plays as a hard cut. Describe the same shot carrying "
                   "on, or make the new shot in \"%s\" instead."
                   % (", ".join("\"%s\"" % f for f in found), ENGINE_MODE_WORDS["fl2va"]))
    return out


ENGINE_MODE_WORDS = {
    "fl2va": "A video from a starting picture (and text)",
    "ref2v": "A video that copies people or clips you upload",
    "continue": "A video that continues the shot before it",
}

H3_MIN_FRAMES, H3_MAX_FRAMES = 124, 362    # the length fields' own range, 17n+5 snapped by the app

_H3_REPLY = (
    "Reply in plain text, no JSON, no markdown, no commentary, in EXACTLY one of these two shapes.\n\n"
    "To ask:\n"
    "QUESTION: <one short question>\n"
    "OPTIONS: <2 to 4 short choices separated by |>\n\n"
    "To write:\n"
    "LENGTH: <NONE, unless the request itself gives a number of seconds: then frames, see below>\n"
    "NOTE: <only when you chose something the user did not say, such as the camera: name each choice>\n"
    "PROMPT: <the finished prompt, one paragraph>\n"
    "PROMPT comes last. Write nothing after it.\n\n"
)
_H3_RULES = (
    "RULES FOR THE PROMPT\n"
    "1. ONE paragraph in the present tense, in this order: the style (\"Live-action, cinematic,\" unless "
    "the user names another, such as \"2D animation,\"); the subject and how it looks; what it does; the "
    "camera; the setting and the light; then the sound: what is heard, in its own sentence.\n"
    "2. Picture and sound are made together. Anything a person says goes INSIDE the prompt, in quotation "
    "marks, with who says it (She says: \"...\"). There is no separate voice.\n"
    "3. The camera is ONE of these moves, in plain words, with a size and a speed when they matter: push "
    "in, pull out, pan left or right, tilt up or down, tracking shot, arc shot, a static shot, a slight "
    "shake (\"The camera pushes in slowly, a small move.\").\n"
    "4. At most two actions. Describe only what IS there: never write \"no ...\", \"without ...\" or "
    "\"don't ...\".\n"
    "5. NEVER write token weights, brackets, tag lists or quality words (\"(dog:1.3)\", \"[snow]\", "
    "\"masterpiece\", \"4k\").\n"
    "6. Keep every subject, name, colour and detail the user gave, and every word they want said, exactly.\n"
)


def _h3_lengths(extra=""):
    return ("\nLENGTH is NONE unless the request gives a number of seconds; an answer to your question is "
            "never a length. When it does: LENGTH = the seconds x 24, never below %d (about 5 seconds) or "
            "above %d (about 15 seconds); the app rounds it to the model's own steps.%s\n"
            % (H3_MIN_FRAMES, H3_MAX_FRAMES, extra))


H3_FL2VA_WRITER_PROMPT = (
    "You write the prompt for a video model that makes one shot, with its sound, from words and an "
    "optional starting picture, from a short request.\n\n"
    + _H3_REPLY + _H3_RULES
    + "7. When the user says a starting picture is set, the prompt carries on from that picture: describe "
      "what happens in it next, naming the subject in the user's own words.\n"
    + _h3_lengths() +
    "\nWHEN TO ASK\n"
    "Most requests need no question: write. Ask ONE question only in the cases below, when neither the "
        "request nor an answer already says it. Ask the first of these that applies:\n"
    "a. ACTION: ONLY when nothing happens in the request at all (\"a lighthouse\", \"a city street\"). "
    "Offer 3-4 things that could happen. Any verb (burning, rolling, runs, waves) is an action: do not "
    "ask this.\n"
    "b. CAMERA: the request has an action but says nothing about the camera. Offer 2-4 camera moves from "
    "rule 3 that suit it.\n"
    "Never ask for more detail about an action the request already gives. Once the user has answered a "
    "question, write; choose anything still open yourself and name it in "
    "NOTE.\n"
    "The room line in [brackets] is the form as it is now; it is not the request.\n\n"
    "EXAMPLE 1\n"
    "Request: a paper lantern on a river\n"
    "QUESTION: What should the lantern do?\n"
    "OPTIONS: Float slowly downstream | Rise into the night sky | Drift into a crowd of lanterns\n\n"
    "EXAMPLE 2\n"
    "Request: a paper lantern on a river\n"
    "The user answered: Float slowly downstream\n"
    "LENGTH: NONE\n"
    "NOTE: I chose a slow tracking shot.\n"
    "PROMPT: Live-action, cinematic, a glowing red paper lantern floats slowly downstream on a dark, calm "
    "river. The camera tracks alongside it at the same slow speed. Willow trees line the banks under a "
    "deep blue dusk sky, the lantern's warm glow rippling on the water. Water laps softly against the "
    "banks and crickets chirp.\n\n"
    "EXAMPLE 3\n"
    "Request: 8 seconds, a street musician finishes a song and thanks the crowd, push in on her\n"
    "LENGTH: %d\n"
    "PROMPT: Live-action, cinematic, a young street musician with a battered acoustic guitar strums the "
    "last chord of her song, then smiles and bows to the small crowd around her. The camera pushes in "
    "slowly, a small move. A brick city square in warm late-afternoon sun. The chord rings out, the crowd "
    "claps, and she says: \"Thank you, you've been lovely.\"\n"
) % (8 * H3_FPS)


H3_REF2V_WRITER_PROMPT = (
    "You write the prompt for a video model that puts the people or things from the user's reference "
    "pictures and clips into a new shot, with its sound, from a short request. You cannot see the "
    "references.\n\n"
    + _H3_REPLY + _H3_RULES
    + "7. Name each referenced person or thing by what the user calls it and say it is the one from the "
      "reference (\"the woman from the reference picture\"). Every one of them gets something to do and "
      "a place in the frame.\n"
      "8. When the request asks someone to speak but gives no words (\"have him announce the winner\"), "
      "write the words yourself in their voice, one or two short sentences.\n"
    + _h3_lengths() +
    "\nWHEN TO ASK\n"
    "Most requests need no question: write. Ask ONE question only in the cases below, when neither the "
        "request nor an answer already says it. Ask the first of these that applies:\n"
    "a. WHO: the request does not say who or what the references are (\"use my pictures\"). Ask what they "
    "show, with no OPTIONS line.\n"
    "b. ACTION: ONLY when the request names who but nothing they do (\"my parrot at a picnic\"). Offer "
    "3-4 things they could do. Any verb (gives a toast, shake hands, dances) is an action: do not ask "
    "this; write, and choose the rest yourself.\n"
    "Never ask for more detail about an action the request already gives. Once the user has answered a "
    "question, write; choose anything still open yourself (the camera too) "
    "and name it in NOTE.\n"
    "The room line in [brackets] is the form as it is now; it is not the request.\n\n"
    "EXAMPLE 1\n"
    "Request: put my dog on a beach\n"
    "QUESTION: What should your dog do on the beach?\n"
    "OPTIONS: Chase the waves | Dig in the sand | Run toward the camera\n\n"
    "EXAMPLE 2\n"
    "Request: the man from my photo opens a birthday present and thanks everyone\n"
    "LENGTH: NONE\n"
    "NOTE: I chose a static shot and wrote his words.\n"
    "PROMPT: Live-action, cinematic, the man from the reference picture sits at a kitchen table, tears "
    "open a small wrapped present and holds up a knitted scarf, beaming. A static shot at eye level. A "
    "bright kitchen with paper streamers, lit by warm window light from the left. Paper rustles, friends laugh, "
    "and he says: \"Oh, you shouldn't have. Thank you, all of you.\"\n"
)


H3_CONTINUE_WRITER_PROMPT = (
    "You write the prompt for the next piece of a shot that is already running: a video model carries on "
    "from the end of the shot before it, keeping its motion and its sound. The prompt must describe the "
    "SAME shot carrying on: the same subject, the same camera move, the same framing, the same place and "
    "light. A new angle or a new composition overrides the carried motion and plays as a hard cut.\n\n"
    + _H3_REPLY + _H3_RULES
    + "7. Say that it carries on: \"keeps ...\", \"goes on ...\", \"still ...\". The camera does what it "
      "did before (\"The camera keeps tracking alongside at the same speed.\").\n"
      "8. NEVER write: cut to, new angle, different angle, reverse shot, new scene, switch to, meanwhile, "
      "we now see, a close-up of, zooms in on.\n"
    + _h3_lengths(" The finished piece comes out about 1 second shorter than LENGTH, so when the user "
                  "gives a number of seconds, add one: 8 seconds is LENGTH %d." % (9 * H3_FPS)) +
    "\nWHEN TO ASK\n"
    "Most requests need no question: write. Ask ONE question only in the cases below, when neither the "
        "request nor an answer already says it. Ask the first of these that applies:\n"
    "a. NEW SHOT: the request asks for a new angle, a new place, a close-up of something else or a jump "
    "in time. QUESTION: Carry on the same shot, or cut to a new one? OPTIONS: Carry on the same shot | "
    "Cut to a new shot\n"
    "b. WHAT: ONLY when the request names no subject at all (\"keep going\", \"continue\"). Ask what the "
    "shot before shows and how its camera moves, with no OPTIONS line. \"The car keeps driving\" names "
    "one: do not ask, write, and keep the camera doing what such a shot does.\n"
    "For \"Carry on the same shot\": keep the shot before exactly as it was, the same framing and camera "
    "(no close-up, no new angle), and let what the user wanted happen inside it where it can. For \"Cut to a new shot\": write the new shot, and say in NOTE that a new shot "
    "belongs in \"%s\", because here it plays as a hard cut.\n"
    "Never ask for more detail about an action the request already gives. Once the user has answered a "
    "question, write; choose anything still open yourself and name it in "
    "NOTE.\n"
    "The room line in [brackets] is the form as it is now; it is not the request.\n\n"
    "EXAMPLE 1\n"
    "Request: now show her face from the front\n"
    "QUESTION: Carry on the same shot, or cut to a new one?\n"
    "OPTIONS: Carry on the same shot | Cut to a new shot\n\n"
    "EXAMPLE 2\n"
    "Request: the cyclist keeps riding up the mountain road\n"
    "LENGTH: NONE\n"
    "NOTE: I kept the camera tracking alongside, as in a riding shot.\n"
    "PROMPT: Live-action, cinematic, the cyclist in the yellow jersey keeps pedalling up the winding "
    "mountain road at the same steady pace. The camera keeps tracking alongside at the same speed, "
    "holding the rider in the centre of the frame. The same pine slopes in bright morning sun. Tyres hum "
    "on the tarmac, the chain clicks, and wind rushes past.\n\n"
    "EXAMPLE 3\n"
    "Request: now show her face from the front\n"
    "The user answered: Carry on the same shot\n"
    "LENGTH: NONE\n"
    "NOTE: Kept the same wide tracking shot; she turns her face toward the camera inside it.\n"
    "PROMPT: Live-action, cinematic, the woman in the green coat keeps walking along the harbour wall at "
    "the same pace and turns her face toward the camera as she goes. The camera keeps tracking alongside "
    "at the same distance, the same wide framing. The same grey morning light over the water. Gulls cry, "
    "her boots tap on the stone, and waves slap the wall.\n"
) % ENGINE_MODE_WORDS["fl2va"]


# The clip fixer (P3c). It sees ONE still from the middle of the clip, never
# the clip: guides/motion/knowledge.md "What the room cannot judge".
H3_REVISER_PROMPT = (
    "You fix a video clip that came out wrong. It was made by a video model (picture and sound together) "
    "from the prompt shown to you, and the user says what is wrong with it. When a picture is attached, "
    "it is ONE still frame from the middle of the clip, not the clip. Reply in EXACTLY this line format, "
    "every key on ONE line. No JSON, no markdown, no commentary.\n\n"
    "QUESTION: <one question, ONLY when the user has not said what is wrong; otherwise blank>\n"
    "DIAGNOSIS: <one sentence: what went wrong and why, tied to a known cause below>\n"
    "FIX: reroll\n"
    "PROMPT: <the whole revised prompt>\n"
    "NOTE: <one sentence on what you changed, or blank>\n"
    "TWEAK: <at most one setting to change, by its name in the form, as a statement; or blank>\n\n"
    "RULES\n"
    "1. ASK OR FIX. When the user says what is wrong, fix it and ask nothing. When they only say it is off "
    "and name nothing (\"it's not right\", \"I don't like it\"), reply with ONLY the QUESTION line, asking "
    "what looks or sounds wrong, and nothing else: never guess a cause.\n"
    "2. A STILL IS NOT THE CLIP. From the still you may describe only what is in that one frame: who and "
    "what is in it, where, the framing, the light. You can NEVER tell from it how anything moved, how the "
    "camera moved, how fast, the sound, the speech, the lip sync or the timing: never say yes or no to any "
    "of those. When the complaint is about one of them, start DIAGNOSIS with \"I can't judge motion or "
    "sound from one still, so going by what you say:\" and fix it from the user's words. Never say whether "
    "a face looks like the person in a reference: tell the user to check it at full size.\n"
    "3. Never describe what you were not shown.\n"
    "4. KNOWN CAUSES on this model. Name the one that fits:\n"
    "   a. A continued shot whose prompt describes a new angle, framing, action or place: it overrides the "
    "carried motion and plays as a hard cut. Fix: describe the same subject, camera move and framing "
    "carrying on, and drop the new part. Words like \"keeps\", \"the same\" and \"carries on\" are "
    "right: keep them. When a still is attached, compare its framing with what the prompt says.\n"
    "   b. A reference that is not named in the prompt binds weakly and gets lost or replaced. Fix: name "
    "each one (\"the woman from the reference picture\") and give it an action and a place.\n"
    "   c. A face from a reference comes out generic or turned away. Fix: say in TWEAK to set Reference "
    "image sizing to max (slower, reads faces much better).\n"
    "   d. Speech missing or wrong: spoken words must be inside the prompt in quotation marks, with who "
    "says them. Fix: put the exact words in.\n"
    "   e. No camera stated, or a vague one. Fix: ONE named move (push in, pull out, pan, tilt, tracking "
    "shot, arc shot, static shot) with a size and speed.\n"
    "   f. Soft detail or a slow unwanted push-in with the quickest Fast tiers. Fix: say in TWEAK to use "
    "Fast best or Best quality.\n"
    "   g. More than two actions, or \"no ...\" / \"without ...\" wording. Fix: one or two actions, and "
    "describe only what is there.\n"
    "5. FIX is always reroll: a clip is made again from the revised prompt, never edited in place.\n"
    "6. THE REVISED PROMPT keeps everything that came out right and changes only what the complaint needs: "
    "one paragraph, present tense: style, subject, action, camera, setting and light, then the sound.\n"
    "7. The examples show the format only; their clips are not the one attached.\n\n"
    "EXAMPLE 1\n"
    "The prompt that made it: The woman walks into the cafe and orders a coffee.\n"
    "The user says: she doesn't look like my reference and she never speaks\n"
    "[1 picture attached.]\n"
    "QUESTION:\n"
    "DIAGNOSIS: I can't judge motion or sound from one still, so going by what you say: the prompt never "
    "says she is the woman from the reference or gives her words, so the reference bound weakly and no "
    "speech was made.\n"
    "FIX: reroll\n"
    "PROMPT: Live-action, cinematic, the woman from the reference picture walks up to the counter of a "
    "small cafe and smiles at the barista. A static shot at eye level. Warm morning light through the "
    "front window. Cups clink and she says: \"A flat white, please.\"\n"
    "NOTE: Named her as the reference and put her words in quotes.\n"
    "TWEAK: Set Reference image sizing to max; check her face at full size afterwards.\n\n"
    "EXAMPLE 2\n"
    "The prompt that made it: A red kite dances above a windy beach.\n"
    "The user says: it's not right\n"
    "[1 picture attached.]\n"
    "QUESTION: What looks or sounds wrong: the kite, how it moves, the camera, or the sound?\n"
)


# Appended to the clip fixer's system prompt only when the helper cannot see
# the still (server.py guide_revise()); a seeing helper copies the opener.
H3_REVISER_BLIND = (
    "NO PICTURE THIS TIME\n"
    "No still from the clip could be shown to you, so never describe it: start DIAGNOSIS with \"I can't "
    "see the clip, so going by what you say:\" and fix it from the user's words.\n"
)


ENGINE = {
    "id": "minimax-h3",
    "cap": "video",
    "cap_order": 2,
    # server.py's api_generate has hand-tuned logic for fl2va/ref2v (frame-
    # grid snapping, turbo-LoRA derivation from step count, ref image/video
    # wiring) that this pack's generic field declarations do not fully
    # capture -- keep it on the legacy path rather than the generic one
    # every other pack now uses.
    "legacy_dispatch": True,
    "roles": {
        "h3_unet_fl2va": ("unet", {"all": ["minimax_h3"], "none": ["ref2v"], "prefer": ["fl2v", "t2v"]}),
        "h3_unet_ref2va": ("unet", {"all": ["minimax_h3", "ref2v"]}),
        "h3_clip_nvfp4": ("clip", {"all": ["qwen3vl", "minimax_h3"], "prefer": ["nvfp4", "awq"]}),
        "h3_clip_int8": ("clip", {"all": ["qwen3vl", "minimax_h3"], "prefer": ["int8"]}),
        "h3_vae_video": ("vae", {"all": ["minimax_h3"], "none": ["audio"], "prefer": ["video"]}),
        "h3_vae_audio": ("vae", {"all": ["minimax_h3", "audio"]}),
        "h3_turbo_lora": ("lora", {"all": ["minimax_h3"], "any": ["turbo", "4step", "lightx2v"],
                                   "prefer": ["comfy", "fl2v"]}),
    },
    "primary": {"fl2va": "h3_unet_fl2va", "ref2v": "h3_unet_ref2va"},
    "cap_from": ["fl2va", "ref2v"],
    "provides": {
        "fl2va": ["h3_unet_fl2va", "h3_vae_video", "h3_vae_audio", ["h3_clip_nvfp4", "h3_clip_int8"]],
        "ref2v": ["h3_unet_ref2va", "h3_vae_video", "h3_vae_audio", ["h3_clip_nvfp4", "h3_clip_int8"]],
        "turbo": ["h3_turbo_lora"],
        # C3.4b: the continue graph is the fl2va unet + MotionContext nodes
        # from the installed NikoDemon80/ComfyUI-H3-Motion-Context pack (no
        # extra model file of its own) -- same models fl2va needs, no more.
        "continue": ["h3_unet_fl2va", "h3_vae_video", "h3_vae_audio", ["h3_clip_nvfp4", "h3_clip_int8"]],
    },
    "words": {
        "h3_unet_fl2va": "the H3 video model",
        "h3_unet_ref2va": "the H3 reference model",
        "h3_clip_nvfp4": "the H3 text encoder",
        "h3_clip_int8": "the H3 text encoder",
        "h3_vae_video": "the H3 video decoder",
        "h3_vae_audio": "the H3 audio decoder",
        "h3_turbo_lora": "the speed pack",
    },
    "graphs": {
        "fl2va": h3_fl2va_graph,
        "ref2v": h3_ref2va_graph,
        "continue": h3_continue_graph,
    },
    "describe": _describe,
    "mode_words": ENGINE_MODE_WORDS,
    "mode_rooms": {"fl2va": "video", "ref2v": "video", "continue": "video"},
    # This engine's own task-type prefix (see _TASK_PREFIX_RE): a beat written
    # for an H3 shot keeps it; copied into another engine's shot, the core
    # takes it off (engines/__init__.py "task_prefix").
    "task_prefix": _TASK_PREFIX_RE.pattern,
    "mode_notes": {
        "fl2va": "animates your picture, with sound, up to about 15 seconds",  # source: our internal component notes, engines/minimax_h3.py:244 (preset note)
        "ref2v": "puts the people or clips you upload into a new scene, with sound",  # source: our internal component notes
        "continue": "continues from an earlier shot, carrying its motion and sound across the cut",  # source: the internal continue-feature commission doc K6 (the ruling names this exact substring)
    },
    # L5: how a prompt must be written for each mode, drawn only from this
    # pack's own docstrings/field hints -- never a new claim.
    "prompt_guides": {
        "fl2va": "Describe the scene and action (positive prompt only); a starting "  # source: engines/minimax_h3.py:23-26 (h3_fl2va_graph docstring), :171 (first_frame hint)
                 "and/or ending picture is optional -- omit both for pure text-to-video.",
        "ref2v": "Describe the scene; H3 speaks any dialogue directly in this text "  # source: engines/minimax_h3.py:67-68 (h3_ref2va_graph docstring)
                 "-- audio is never fed in as TTS.",
        "continue": "Describe the SAME shot carrying on -- the same subject, the same "  # source: our internal l2-continuity-2026-09-23 measurement notes, section "Live-app finding"
                    "camera move and the same framing as the shot before it, never a "
                    "new angle or composition: a new composition overrides the ~0.9 s "
                    "of carried motion. Example (H3's structured format): "
                    "integrated_multimodal_description: \"[Shot 1] Live-action, cinematic, "
                    "a red vintage sports car keeps driving from left to right along a "
                    "coastal road at the same steady speed in bright afternoon sun. The "
                    "camera tracks alongside the car at the same speed, keeping it "
                    "centred in frame.\" overall_soundscape: \"The car's engine hums at a "
                    "steady pitch, tyres roll on warm asphalt, wind rushes past.\" "
                    "non_diegetic_music: \"None.\" No starting picture is needed -- the "
                    "shot before it carries the visual and audio continuation. (See "
                    "our internal l2-continuity-2026-09-23 measurement notes, \"Live-app "
                    "finding\".)",
    },
    # P3c: the Motion guide's writing skills (engines/__init__.py "writers").
    "writers": {
        "fl2va": {"label": "Shot writer", "prompt": H3_FL2VA_WRITER_PROMPT,
                  "keys": {"LENGTH": "length", "PROMPT": "prompt"},
                  "multiline": "PROMPT", "none_token": "NONE", "check": h3_shot_check},
        "ref2v": {"label": "Reference shot writer", "prompt": H3_REF2V_WRITER_PROMPT,
                  "keys": {"LENGTH": "length", "PROMPT": "prompt"},
                  "multiline": "PROMPT", "none_token": "NONE", "check": h3_shot_check},
        "continue": {"label": "Carry-on writer", "prompt": H3_CONTINUE_WRITER_PROMPT,
                     "keys": {"LENGTH": "length", "PROMPT": "prompt"},
                     "multiline": "PROMPT", "none_token": "NONE", "check": h3_continue_check},
    },
    # P3c: "Not right? Tell the guide" on a finished clip (engines/__init__.py
    # "revisers"). A clip is never edited in place, so reroll is the only fix.
    "revisers": {
        mode: {"label": "Clip fixer", "prompt": H3_REVISER_PROMPT, "blind_note": H3_REVISER_BLIND,
               "keys": ["QUESTION", "DIAGNOSIS", "FIX", "PROMPT", "NOTE", "TWEAK"],
               "fills": "prompt", "edit_mode": None, "fixes": ["reroll"]}
        for mode in ("fl2va", "ref2v", "continue")
    },
    # R4: describes the surface server.py's kind=="video" branch already
    # sends (lines ~1488-1552) -- defaults/ranges taken from there, not
    # invented. `length` is FRAMES (H3's 17n+5 grid via snap_frames), not
    # seconds -- the seconds shown to the user is length/24.
    "fields": {
        "fl2va": [
            {"id": "prompt", "label": "Prompt", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "first_frame", "label": "Starting picture", "type": "image",
             "tier": "primary", "group": "Content", "order": 2,
             "hint": "Optional. Omit both frames for pure text-to-video."},
            {"id": "last_frame", "label": "Ending picture", "type": "image",
             "tier": "advanced", "group": "Content", "order": 3},
            # Frame grid is 17n+5; 362 = 15.1s trained max, 124 = ~5s min.
            {"id": "length", "label": "Length (frames)", "type": "int", "default": 362,
             "tier": "primary", "group": "Length", "order": 1,
             "units": "frames", "range": [124, 362], "ui_range": [124, 362],
             "hint": "About 5 to 15 seconds. The length snaps to the nearest step the model supports."},
            {"id": "width", "label": "Width", "type": "int", "default": 960,
             "tier": "advanced", "group": "Size", "order": 1,
             "units": "px", "range": [128, 1920], "ui_range": [512, 1280],
             "hint": "Snapped to a multiple of 32."},
            {"id": "height", "label": "Height", "type": "int", "default": 544,
             "tier": "advanced", "group": "Size", "order": 2,
             "units": "px", "range": [128, 1920], "ui_range": [288, 720],
             "hint": "Snapped to a multiple of 32."},
            {"id": "steps", "label": "Steps", "type": "int", "default": 20,
             "tier": "advanced", "group": "Quality", "order": 1,
             "units": "steps", "range": [1, 80], "ui_range": [8, 30]},
            # A 4-step distillation LoRA -- fights the sampling schedule and smooths
            # detail away above 8 steps, which is why it force-disables itself there.
            {"id": "turbo_lora", "label": "Fast mode (turbo)", "type": "checkbox",
             "tier": "advanced", "group": "Quality", "order": 2,
             "hint": "A faster mode that only works at low detail settings. "
                     "Turns itself off above 8 steps."},
            # Defaults to the NVFP4 AWQ encoder here; ref2v below defaults to INT8.
            {"id": "encoder", "label": "Text encoder override", "type": "text",
             "tier": "advanced", "group": "Quality", "order": 3,
             "hint": "Leave blank to use the default."},
        ],
        # C3.4b K5: same base recipe as fl2va, but the "video" field is a
        # jack (server.py's slot_jacks/_op_patch), not an upload -- its
        # value only ever arrives already patched in from the shot before
        # it on the timeline, never typed or chosen here.
        "continue": [
            {"id": "prompt", "label": "Prompt", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "prev_video", "label": "Previous shot", "type": "video",
             "tier": "primary", "group": "Content", "order": 2,
             "hint": "Plug in the shot before this one on the timeline."},
            # Frame grid is 17n+5; 362 = 15.1s trained max, 124 = ~5s min.
            # The delivered clip is 22 frames shorter than this: the join
            # keeps the previous shot's last frames, then trims them back
            # off the end once the new ones are made (L2's own finding).
            {"id": "length", "label": "Length (frames)", "type": "int", "default": 362,
             "tier": "primary", "group": "Length", "order": 1,
             "units": "frames", "range": [124, 362], "ui_range": [124, 362],
             "hint": "About 5 to 15 seconds. The delivered shot comes out 22 "
                     "frames (about 0.9s) shorter than this."},
            {"id": "width", "label": "Width", "type": "int", "default": 960,
             "tier": "advanced", "group": "Size", "order": 1,
             "units": "px", "range": [128, 1920], "ui_range": [512, 1280],
             "hint": "Snapped to a multiple of 32."},
            {"id": "height", "label": "Height", "type": "int", "default": 544,
             "tier": "advanced", "group": "Size", "order": 2,
             "units": "px", "range": [128, 1920], "ui_range": [288, 720],
             "hint": "Snapped to a multiple of 32."},
            {"id": "steps", "label": "Steps", "type": "int", "default": 20,
             "tier": "advanced", "group": "Quality", "order": 1,
             "units": "steps", "range": [1, 80], "ui_range": [8, 30]},
            {"id": "turbo_lora", "label": "Fast mode (turbo)", "type": "checkbox",
             "tier": "advanced", "group": "Quality", "order": 2,
             "hint": "A faster mode that only works at low detail settings. "
                     "Turns itself off above 8 steps."},
            {"id": "encoder", "label": "Text encoder override", "type": "text",
             "tier": "advanced", "group": "Quality", "order": 3,
             "hint": "Leave blank to use the default."},
        ],
        "ref2v": [
            {"id": "prompt", "label": "Prompt", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "ref_images", "label": "Reference pictures", "type": "image_list",
             "tier": "primary", "group": "Content", "order": 2, "max": 9,
             "hint": "Up to 9. At least one picture or clip is required."},
            {"id": "ref_videos", "label": "Reference clips", "type": "video_list",
             "tier": "primary", "group": "Content", "order": 3,
             "hint": "Up to 3, 2-15s each at 24fps. The first clip's audio can be kept."},
            # Frame grid is 17n+5; 362 = 15.1s trained max, 124 = ~5s min.
            {"id": "length", "label": "Length (frames)", "type": "int", "default": 362,
             "tier": "primary", "group": "Length", "order": 1,
             "units": "frames", "range": [124, 362], "ui_range": [124, 362],
             "hint": "About 5 to 15 seconds. The length snaps to the nearest step the model supports."},
            {"id": "width", "label": "Width", "type": "int", "default": 960,
             "tier": "advanced", "group": "Size", "order": 1,
             "units": "px", "range": [128, 1920], "ui_range": [512, 1280]},
            {"id": "height", "label": "Height", "type": "int", "default": 544,
             "tier": "advanced", "group": "Size", "order": 2,
             "units": "px", "range": [128, 1920], "ui_range": [288, 720]},
            {"id": "steps", "label": "Steps", "type": "int", "default": 20,
             "tier": "advanced", "group": "Quality", "order": 1,
             "units": "steps", "range": [1, 80], "ui_range": [8, 30]},
            {"id": "keep_audio", "label": "Keep audio from first reference clip",
             "type": "checkbox", "default": True,
             "tier": "advanced", "group": "Quality", "order": 2},
            # Live node schema (GET /object_info/MiniMaxH3ReferenceToVideo, queried
            # 2026-09-22): options are exactly ["match", "max"], default "match".
            # "match" scales each reference down-only to the generation's pixel
            # area; "max" uses the reference pipeline's 2048px short edge for best
            # identity fidelity. Matches the "max-reference" preset's measured
            # 161.7s vs 90.2s (1.8x), not benchmarked further than that.
            {"id": "ref_image_size", "label": "Reference image sizing", "type": "select",
             "default": "match", "options": ["match", "max"],
             "tier": "advanced", "group": "Quality", "order": 3,
             "hint": "Max reads faces much better and is slower (measured 1.8x)."},
            # Defaults to the INT8 encoder here (fl2va above defaults to NVFP4).
            {"id": "encoder", "label": "Text encoder override", "type": "text",
             "tier": "advanced", "group": "Quality", "order": 4,
             "hint": "Leave blank to use the default."},
        ],
    },
    # R3: named parameter sets, same discipline as engines/audio.py's presets
    # -- notes cite real measurements, not a guess.
    "presets": {
        "fl2va": [
            {"id": "default-length", "label": "Standard length (default)", "note":
             "362 frames (about 15.1s) is the trained max and this field's own default.",
             "values": {"length": 362}},
            # Full citation: 8 steps with the speed pack measured +2.30 dB over the
            # 4-step version (5/5 seeds, paired t=12.08) and removed a 1.04-1.10x
            # push-in drift entirely (1.000x at 8 steps). Deployed end-to-end:
            # +5.0 dB and no drift vs the prior broken state. Measured 2026-09-21,
            # internal measurement notes.
            {"id": "turbo-8step", "label": "Fast (8-step, measured)", "note":
             "8 steps with the speed pack measured a clear quality gain over the "
             "4-step version and removed a small drift entirely. Measured 2026-09-21.",
             "values": {"steps": 8, "turbo_lora": True}},
        ],
        "ref2v": [
            {"id": "default-length", "label": "Standard length (node default)", "note":
             "124 frames is this mode's own default and the floor of its trained "
             "range (about 124-362).",
             "values": {"length": 124}},
            # Full citation: ref_image_size="max" measured 161.7s vs 90.2s render
            # (1.8x, not the tooltip's "several times") but reads referenced faces
            # far better -- "match" rendered a character near-profile and generic.
            # No 8-step speed pack exists for this path (only a 4-step one), so
            # that win does not transfer here. Measured,
            # internal measurement notes.
            {"id": "max-reference", "label": "Best face fidelity (measured)", "note":
             "Reads referenced faces far better than the default, and is slower "
             "(measured 1.8x). The default rendered a character near-profile and generic.",
             "values": {"ref_image_size": "max"}},
        ],
        # C3.4b: not a dedicated A/B for continue specifically -- every arm
        # of the L2 spike (including this one) rendered at 8 steps with the
        # speed pack; no separate measurement exists yet for this mode
        # alone. Our internal l2-continuity-2026-09-23 measurement notes.
        "continue": [
            {"id": "default-length", "label": "Standard length (default)", "note":
             "362 frames (about 15.1s) is the trained max and this field's own default.",
             "values": {"length": 362}},
            {"id": "turbo-8step", "label": "Fast (8-step, spike setting)", "note":
             "8 steps with the speed pack is what every test render of this mode used.",
             "values": {"steps": 8, "turbo_lora": True}},
        ],
    },
    # R2: ported verbatim from the old page's VID_Q / VID_Q_WHY constants
    # (index.html, since replaced) -- same six step counts, same "why" text,
    # Best still the default. The 4/6/8-step rows carry "requires": "turbo"
    # and set turbo_lora explicitly (not just steps<=8) so their `values`
    # produce exactly what api_generate's video branch already derives --
    # see its comment at the `turbo = ...` line. unavailable_reason is the
    # old page's own applySpeedPack() sentence.
    #
    # RULING (owner, after C2a review): ref2v does NOT get the three fast
    # rows. h3_ref2va_graph (above) never reads p["turbo_lora"] -- there is
    # no speed pack wired for this path -- so a "Fast" tier here would run
    # 4-8 steps with no distillation LoRA, exactly the mush the old page's
    # applySpeedPack() switched those rows off to prevent. A 4-step
    # `minimax_h3_ref2v_turbo_4step` exists upstream; wiring it in is a
    # separate change, not this one. ref2v keeps Middle/Good/Best only.
    "quality": {
        "fl2va": [
            {"id": "fast", "label": "Fast", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, quickest, good for trying an idea",
             "values": {"steps": 4, "turbo_lora": True}},
            {"id": "fast-plus", "label": "Fast+", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, a little more detail",
             "values": {"steps": 6, "turbo_lora": True}},
            {"id": "fast-best", "label": "Fast best", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, best of the quick ones",
             "values": {"steps": 8, "turbo_lora": True}},
            {"id": "middle", "label": "Middle",
             "why": "runs clean, in between, can look soft",
             "values": {"steps": 12, "turbo_lora": False}},
            {"id": "good", "label": "Good",
             "why": "runs clean, close to best, a bit quicker",
             "values": {"steps": 16, "turbo_lora": False}},
            {"id": "best", "label": "Best", "default": True,
             "why": "runs clean, what every good clip we made used",
             "values": {"steps": 20, "turbo_lora": False}},
        ],
        # No fast tiers, no "turbo_lora" key: see the ruling above ref2v does
        # not have a speed pack wired in, so it starts at Middle.
        "ref2v": [
            {"id": "middle", "label": "Middle",
             "why": "runs clean, in between, can look soft",
             "values": {"steps": 12}},
            {"id": "good", "label": "Good",
             "why": "runs clean, close to best, a bit quicker",
             "values": {"steps": 16}},
            {"id": "best", "label": "Best", "default": True,
             "why": "runs clean, what every good clip we made used",
             "values": {"steps": 20}},
        ],
        # Same tiers as fl2va, same base recipe -- continue's own graph adds
        # only the previous-shot jack, no separate quality axis.
        "continue": [
            {"id": "fast", "label": "Fast", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, quickest, good for trying an idea",
             "values": {"steps": 4, "turbo_lora": True}},
            {"id": "fast-plus", "label": "Fast+", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, a little more detail",
             "values": {"steps": 6, "turbo_lora": True}},
            {"id": "fast-best", "label": "Fast best", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, best of the quick ones",
             "values": {"steps": 8, "turbo_lora": True}},
            {"id": "middle", "label": "Middle",
             "why": "runs clean, in between, can look soft",
             "values": {"steps": 12, "turbo_lora": False}},
            {"id": "good", "label": "Good",
             "why": "runs clean, close to best, a bit quicker",
             "values": {"steps": 16, "turbo_lora": False}},
            {"id": "best", "label": "Best", "default": True,
             "why": "runs clean, what every good clip we made used",
             "values": {"steps": 20, "turbo_lora": False}},
        ],
    },
    # H2: "Try this" -- fl2va's starting picture is optional (pure
    # text-to-video works); ref2v needs at least one reference picture first;
    # continue needs an earlier shot already made and picked, not a file
    # this example can preset (the jack is filled by a cable, never typed).
    "examples": {
        "fl2va": [
            {"id": "fl2va-try", "label": "A video from an idea", "recipe": "default-length",
             "quality": "best",
             "values": {"prompt": "a paper lantern floating gently down a river at dusk"},
             "why": "a video animated from a starting idea, with sound", "needs": None},
        ],
        "ref2v": [
            {"id": "ref2v-try", "label": "A video with your reference", "recipe": "default-length",
             "quality": "best",
             "values": {"prompt": "the person steps into a sunlit garden"},
             "why": "a video that places your reference into a new scene",
             "needs": "picture"},
        ],
        "continue": [
            {"id": "continue-try", "label": "Continue the shot before", "recipe": "default-length",
             "quality": "best",
             "values": {"prompt": "the car keeps driving along the coast road"},
             "why": "carries the motion and sound of the shot before it into this one",
             "needs": "previous shot"},
        ],
    },
    "licence": {
        "name": "MiniMax-H3 Model License",
        "shippable": False,
        "attribution": "MiniMax-H3 by MiniMax",
        "url": "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE",
        "note": "Open weights are licensed outside the EU, UK, South Korea and the USA; "
                "elsewhere MiniMax asks you to apply (platform.minimax.io/h3-license).",
    },
}
