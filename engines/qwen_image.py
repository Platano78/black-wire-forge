"""Engine pack: Qwen-Image 2.1 (text-to-image and edit).

Copied from server.py's pre-extraction ROLE_RULES/ROLE_POOL entries,
abilities()/missing_for() labels, describe_image(), and the qwen_*_graph
builders. Logic is verbatim; only the lane->models indirection is replaced
by the contract's direct ``models`` dict.
"""
import re

from . import unet_loader, quant_words


def _describe(models):
    name = (models.get("qwen_unet") or "").lower()
    ver = " 2.1" if "2.1" in name else ""
    q = quant_words(name)
    return "Qwen-Image" + ver + (" (%s)" % q if q else "")


def qwen_t2i_graph(p, m):
    # Q21: "Balanced" (the A/B's arm D, measured 2026-09-23,
    # our internal CFG/APG/FreSca A/B notes) is t2i's
    # own default guidance style -- APG then FreSca sit between the UNET
    # and KSampler, and the sampler/scheduler defaults change with it.
    # "Plain" is today's graph exactly (plain CFG, euler/simple). Defaults
    # here mirror this mode's field declarations below; the field ids stay
    # the single source of truth for the actual default values.
    guidance_style = p.get("guidance_style") or "Balanced"
    sampler_name = p.get("sampler") or "seeds_2"
    scheduler = p.get("scheduler") or "sgm_uniform"
    g = {
        "1": unet_loader(m["qwen_unet"]),
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["qwen_clip"], "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["qwen_vae"]}},
        # 2.1 native: TextEncodeQwenImage21 emits positive AND negative from one node.
        # The latent still comes from EmptyLatentImage here -- for t2i there is no
        # reference, so the encoder's own latent carries no size we want. (The EDIT
        # graph below DOES use the encoder's latent, and must.)
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["2", 0], "prompt": p["prompt"], "negative_prompt": p.get("negative", ""),
            "resolution": int(p.get("resolution", 1024)), "vae": ["3", 0]}},
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": p["width"], "height": p["height"], "batch_size": 1}},
    }
    model_out = ["1", 0]
    if guidance_style == "Balanced":
        g["11"] = {"class_type": "APG", "inputs": {
            "model": ["1", 0],
            "eta": float(p.get("apg_eta", 1.0)),
            "norm_threshold": float(p.get("apg_norm_threshold", 10.0)),
            "momentum": float(p.get("apg_momentum", 0.3))}}
        g["12"] = {"class_type": "FreSca", "inputs": {
            "model": ["11", 0],
            "scale_low": float(p.get("fresca_scale_low", 1.0)),
            "scale_high": float(p.get("fresca_scale_high", 2.0)),
            "freq_cutoff": int(p.get("fresca_freq_cutoff", 8))}}
        model_out = ["12", 0]
    g["8"] = {"class_type": "KSampler", "inputs": {
        "model": model_out, "positive": ["4", 0], "negative": ["4", 1], "latent_image": ["6", 0],
        "seed": p["seed"], "steps": p["steps"], "cfg": p["cfg"],
        "sampler_name": sampler_name, "scheduler": scheduler, "denoise": 1.0}}
    g["9"] = {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}}
    g["10"] = {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "blackwire/IMG"}}
    return g


def qwen_edit_graph(p, m):
    """Qwen-Image-2.1 edit. TextEncodeQwenImage21 is the 2.1-native encoder: it
    takes the reference images, emits positive + negative conditioning AND the
    matched latent (its own tooltip: any other latent size shifts the edit)."""
    refs = p.get("ref_images") or []
    if not refs:
        raise ValueError("Add at least one picture to work from.")
    # Q21: edit's Default stays "Plain" (arm D was never measured on edit --
    # switching edit's own default is an open owner question). The
    # guidance_style/sampler/scheduler fields exist here too so a caller CAN
    # ask for "Balanced", but absent they resolve to exactly today's graph.
    guidance_style = p.get("guidance_style") or "Plain"
    sampler_name = p.get("sampler") or "euler"
    scheduler = p.get("scheduler") or "simple"
    g = {
        "1": unet_loader(m["qwen_unet"]),
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["qwen_clip"], "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["qwen_vae"]}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["2", 0], "prompt": p["prompt"], "negative_prompt": p.get("negative", ""),
            "resolution": int(p.get("resolution", 1024)), "vae": ["3", 0]}},
    }
    model_out = ["1", 0]
    if guidance_style == "Balanced":
        g["11"] = {"class_type": "APG", "inputs": {
            "model": ["1", 0],
            "eta": float(p.get("apg_eta", 1.0)),
            "norm_threshold": float(p.get("apg_norm_threshold", 10.0)),
            "momentum": float(p.get("apg_momentum", 0.3))}}
        g["12"] = {"class_type": "FreSca", "inputs": {
            "model": ["11", 0],
            "scale_low": float(p.get("fresca_scale_low", 1.0)),
            "scale_high": float(p.get("fresca_scale_high", 2.0)),
            "freq_cutoff": int(p.get("fresca_freq_cutoff", 8))}}
        model_out = ["12", 0]
    # MEASURED 2026-09-22, do not remove as dead weight: it is a REAL knob.
    # Identical edit graph, same seed and reference, with vs without this node:
    # max pixel delta 69, correlation +0.9925. With it the glow is slightly
    # brighter and more saturated. Our own internal edit graph omits it;
    # neither output is wrong, so upstream's default is kept and the difference
    # is recorded rather than guessed at. Owner eye-gate if it ever matters.
    g["7"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": model_out, "shift": 3.1}}
    g["8"] = {"class_type": "KSampler", "inputs": {
        "model": ["7", 0], "positive": ["4", 0], "negative": ["4", 1], "latent_image": ["4", 2],
        "seed": p["seed"], "steps": p["steps"], "cfg": p["cfg"],
        "sampler_name": sampler_name, "scheduler": scheduler, "denoise": 1.0}}
    g["9"] = {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}}
    g["10"] = {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "blackwire/EDIT"}}
    for i, name in enumerate(refs[:10], start=1):
        nid = str(100 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g["4"]["inputs"]["images.image_%d" % i] = [nid, 0]
    return g


# The picture fixer's system prompt (P2b). The Godzilla example is the live
# case: job c78e51eab415's render, inspected at full resolution, shows THREE
# creatures (two Godzilla-like kaiju and a single-headed winged dragon) for a
# prompt that named "Godzilla and MechaKing Ghidorah" -- guides/picture/skills.md.
PICTURE_REVISER_PROMPT = (
    "You fix a picture that came out wrong. It was made by a text-to-picture model from the prompt "
    "shown to you, and the user says what is wrong with it. When a picture is attached, it IS that "
    "render. Reply in EXACTLY this line format, every key on ONE line. No JSON, no markdown, no "
    "commentary.\n\n"
    "QUESTION: <one question, ONLY when the user has not said what is wrong; otherwise blank>\n"
    "DIAGNOSIS: <one sentence: what went wrong and why, tied to a known cause below>\n"
    "FIX: <reroll or edit>\n"
    "PROMPT: <reroll: the whole revised prompt; edit: the edit instruction>\n"
    "NOTE: <one sentence on what you changed, or blank>\n"
    "TWEAK: <at most one optional suggestion, a statement and never a question, or blank>\n\n"
    "RULES\n"
    "1. ASK, CHECK OR FIX. When the user says what is wrong (\"the dragon has one head\", \"too dark\", "
    "\"I asked for two and got three\"), fix it and ask nothing. When they ask you to check it (\"is "
    "anything wrong?\", \"does it look right?\") and a picture is attached, look at it, compare it with "
    "the prompt and answer: do not ask. When they only say it is off (\"it's not right\", \"I don't "
    "like it\") and name nothing, reply with ONLY the QUESTION line, asking what looks wrong (with no "
    "picture, start it as rule 2 says), and nothing else.\n"
    "2. SEE ONLY WHAT IS THERE. The last line of the message says whether a picture is attached.\n"
    "   With a picture: name only a defect you could point at in it. If it shows what the prompt asks "
    "for and nothing is wrong, say so in DIAGNOSIS (\"Nothing looks wrong: it shows what the prompt "
    "asks for.\"), FIX reroll, and PROMPT is the same prompt unchanged. Never invent a defect. A count "
    "past three things is a rough read.\n"
    "   With NO picture you cannot see the render: never describe it. If the user's words say what is "
    "wrong, start DIAGNOSIS with \"I can't see the picture, so going by what you say:\" and fix it. "
    "If they do not, ask with QUESTION, starting \"I can't see the picture, so tell me:\", what it "
    "shows that is wrong.\n"
    "3. KNOWN CAUSES on this model. Name the one that fits:\n"
    "   a. A NAMED subject the model may not know (a character, a franchise monster, a brand): the bare "
    "name renders as a guess, often a copy of another subject in the frame or an extra, unrequested "
    "one. Fix: describe it by appearance: body, colour, material, number of heads, wings or limbs.\n"
    "   b. An UNSTATED COUNT: \"a battle between X and Y\" does not say how many. Fix: state every "
    "count, e.g. \"exactly two creatures\", \"one person\".\n"
    "   c. Things to avoid is set but Guidance strength is below 2.5, so it barely works. Fix: say in "
    "NOTE to raise Guidance strength to 2.5 or more; the prompt stays positive.\n"
    "   d. No colour or material given, so the model guessed. Fix: a colour with a modifier and a "
    "material (\"deep navy\", \"brushed steel\").\n"
    "   e. In an edit, the wrong picture was treated as the canvas. Fix: name which picture changes and "
    "which is only a reference (\"change picture 1; use picture 2 only for the jacket\").\n"
    "4. REROLL OR EDIT. REROLL (a new picture from a revised prompt) when the subject, a count or the "
    "composition is wrong. EDIT only when the picture is right except for one local thing (a colour, "
    "one object, the background); then PROMPT is a short instruction for that one change.\n"
    "5. THE REVISED PROMPT keeps everything that came out right, fixes what went wrong, and is "
    "positive only (never \"no X\": that belongs in Things to avoid). Present tense: one opening "
    "sentence naming style, subject and setting, then the frame from left to right, one lighting "
    "sentence, one composition sentence.\n"
    "6. Never claim to see what is not in the attached picture. A video arrives as one still frame: "
    "never claim to have watched it.\n"
    "7. The examples below show the format only. Their pictures are not the one attached: describe "
    "only what you see in yours.\n\n"
    "EXAMPLE 1\n"
    "The prompt that made it: A cinematic battle between Godzilla and MechaKing Ghidorah in a ruined city.\n"
    "The user says: the second monster doesn't look right: it's not gold, doesn't have three heads, "
    "and isn't mechanical.\n"
    "[1 picture attached.]\n"
    "QUESTION:\n"
    "DIAGNOSIS: The model doesn't reliably know \"MechaKing Ghidorah\" by name, so it drew two "
    "Godzilla-like kaiju and an extra single-headed winged dragon: three creatures, none golden, "
    "three-headed or mechanical.\n"
    "FIX: reroll\n"
    "PROMPT: A cinematic realistic scene of a battle between exactly two giant monsters on a ruined "
    "city street at night: on the left a large grey reptilian kaiju with spiked dorsal plates, "
    "mid-roar; on the right a golden three-headed mechanical dragon with bat-like metal wings and "
    "armour plating, lightning arcing between its three heads. Rubble and fire fill the foreground. "
    "The lighting is harsh orange fire-glow against a dark, smoke-filled sky. The composition is "
    "high-contrast and centred on the clash between the two creatures.\n"
    "NOTE: Described the second monster by its appearance instead of its name, and pinned the count "
    "to exactly two.\n"
    "TWEAK:\n\n"
    "EXAMPLE 2\n"
    "The prompt that made it: A red fox sitting in a snowy meadow at dawn.\n"
    "The user says: it's just off\n"
    "[1 picture attached.]\n"
    "QUESTION: What looks wrong to you: the fox, the snow, the light, or the framing?\n\n"    "EXAMPLE 3\n"
    "The prompt that made it: A red fox sitting in a snowy meadow at dawn.\n"
    "The user says: it's just off\n"
    "[The user attached a picture, but this helper cannot see pictures.]\n"
    "QUESTION: I can't see the picture, so tell me: what looks wrong with the fox, the snow, the light "
    "or the framing?\n\n"
    "EXAMPLE 4\n"
    "The prompt that made it: A white ceramic mug on a wooden table.\n"
    "The user says: the mug is perfect but I wanted the table dark walnut.\n"
    "[The user attached a picture, but this helper cannot see pictures.]\n"
    "QUESTION:\n"
    "DIAGNOSIS: I can't see the picture, so going by what you say: the prompt gave the table no "
    "colour, so the model picked one.\n"
    "FIX: edit\n"
    "PROMPT: Make the wooden table dark walnut and keep the mug and everything else unchanged.\n"
    "NOTE: Only the table changes, so an edit keeps the mug you like.\n"
    "TWEAK:\n"
)


# ── the Picture guide's writers (engines/__init__.py "writers") ──────────────
# Rules restated in our own words from guides/picture/skills.md Skill 1 and
# the engine's upstream prompt enhancer (research licence: never copied). The
# enhancer turns a request into one long paragraph describing the finished
# frame, with the shape chosen separately; a generic brain gets that as a
# line-delimited reply (the JSON lesson from the song expander).

# The shapes the writer may pick, as width x height: each side a multiple of
# 16 near the 1328x1328 default's pixel count, so every shape costs about the
# same to render.
T2I_SHAPES = (
    ("square 1:1", 1328, 1328), ("wide 16:9", 1664, 928), ("tall 9:16, a phone screen", 928, 1664),
    ("landscape 4:3", 1472, 1104), ("portrait 3:4", 1104, 1472), ("photo 3:2", 1584, 1056),
    ("photo 2:3", 1056, 1584), ("cinema 21:9", 1904, 816),
)
# "no people" in a positive prompt draws people: the words belong in Things to avoid.
_NEGATION_RE = re.compile(r"\b(no|not|without|avoid|don't|never)\b", re.IGNORECASE)
T2I_MIN_WORDS = 40


def t2i_check(values, request):
    """The t2i writer's check on its own drafts (never at Make time: a
    person's own short prompt is theirs). -> plain problem sentences."""
    prompt = values.get("prompt") or ""
    problems = []
    found = sorted({m.group(1).lower() for m in _NEGATION_RE.finditer(prompt)})
    if found:
        problems.append("The prompt says %s: this model draws what the words name, so a thing named there "
                        "tends to appear. Keep the prompt positive and put what to leave out in Things to "
                        "avoid." % ", ".join('"%s"' % w for w in found))
    if len(prompt.split()) < T2I_MIN_WORDS:
        problems.append("The prompt is only %d words: describe the whole finished frame (who or what is "
                        "where, colours and materials, the light, the composition) in about 80 to 150 words."
                        % len(prompt.split()))
    size = (values.get("width"), values.get("height"))
    if None not in size and size not in {(w, h) for _, w, h in T2I_SHAPES}:
        problems.append("Width %s and height %s are not one of the shapes listed: pick one line from the "
                        "list for both." % size)
    return problems


T2I_WRITER_PROMPT = (
    "You write the prompt for a text-to-picture model from a short request.\n"
    "How it works: the model draws exactly what the words describe. It does not know most names, it "
    "does not count unless told, and it cannot ask.\n\n"
    "Reply in plain text, no JSON, no markdown, no commentary, in EXACTLY one of these two shapes.\n\n"
    "To ask:\n"
    "QUESTION: <one short question>\n"
    "OPTIONS: <2 to 5 short choices separated by |>\n\n"
    "To write:\n"
    "WIDTH: <NONE, unless the request states a shape or a use with a shape: then the width from SHAPES. "
    "A request that names neither is NONE: the form keeps its own size>\n"
    "HEIGHT: <NONE, or the height from the SAME line of SHAPES>\n"
    "NEGATIVE: <NONE, unless the request names things to leave out: then just those things, "
    "comma-separated, without the word no>\n"
    "NOTE: <only when you chose something the user did not say, such as the style or the setting: "
    "name each choice>\n"
    "PROMPT: <the finished prompt, one paragraph>\n"
    "PROMPT comes last. Write nothing after it.\n\n"
    "SHAPES (width x height)\n"
    + "".join("%s = %d x %d\n" % shape for shape in T2I_SHAPES) +
    "\nRULES FOR THE PROMPT\n"
    "1. Describe the FINISHED picture as if you are looking at it: present tense, third person. Never "
    "\"create\", \"generate\", \"an image of\" or \"you\".\n"
    "2. Open with one sentence naming the style or medium, the subject and the setting (\"A realistic "
    "cinematic photograph of ...\", \"A bright 3D animated film still of ...\").\n"
    "3. Then walk the frame: what is on the left, in the centre, on the right, in front and behind; what "
    "each thing is doing; its colours and materials.\n"
    "4. Then one sentence on the light, and one closing sentence on the composition and mood.\n"
    "5. COUNTS: say how many of every person, animal, creature or object that matters (\"exactly two "
    "giant monsters\", \"one small dog\"). A fight or a meeting between named subjects has exactly that "
    "many of their kind in the frame; say so.\n"
    "6. NAMED SUBJECTS: the model does not reliably know characters, franchise monsters, mascots, "
    "celebrities or brands by name, and draws a guess, often a copy of another subject in the frame. "
    "Keep the name AND describe how it looks: body shape, size, colours, material, the number of heads, "
    "wings, arms or legs, and the two or three features that make it recognisable. Describe each named "
    "subject in its own sentence, with its place in the frame, so two are never mixed up.\n"
    "7. Colours get a modifier (deep navy, pale gold); things get a material (brushed steel, worn leather).\n"
    "8. Positive only: never write no, not, without, avoid or never in PROMPT. What to leave out goes in "
    "NEGATIVE.\n"
    "9. Never write a ratio, a resolution or a pixel size in PROMPT: the shape is WIDTH and HEIGHT.\n"
    "10. A short request still gets a full description of about 80 to 150 words: invent sensible detail "
    "for everything it leaves open. A request that is already detailed keeps its own words: order and "
    "clarify them, add no new subjects.\n"
    "11. Keep every subject, name, count, colour and detail the user gave. Only words that must appear "
    "as TEXT in the picture (a sign, a title) go in double quotes, exactly as given: quoted words are "
    "drawn as lettering.\n\n"
    "WHEN TO ASK\n"
    "Ask ONE question, only when its answer changes the picture a lot and neither the request nor an "
    "answer says it. Ask the first of these that applies:\n"
    "a. SUBJECT: the request is about something words cannot tell the model: the user's own pet, "
    "person, car or house (\"my dog\", \"my son\"). Ask what it looks like; offer 3-4 typical looks.\n"
    "b. COUNT: a group of the user's own people or things with no number (\"my kids\", \"our team\").\n"
    "c. STYLE: the request puts a cartoon or anime character beside a real person (a cartoon mouse and "
    "a real athlete) and does not say whether the picture is realistic or drawn. OPTIONS: Live action | "
    "Cartoon | 3D animated. Two monsters, or characters from one world, are no reason to ask: choose "
    "the style yourself.\n"
    "d. SHAPE: the request names a use whose shape it does not give (a poster, a cover, a banner, a "
    "wallpaper). Offer 2-3 shapes from SHAPES.\n"
    "Never ask about the light, the camera, the colours or any detail you can choose: choose it, and "
    "name the big choices in NOTE. Once the user has answered, write.\n"
    "The room line in [brackets] is the form as it is now; it is not the request.\n\n"
    "EXAMPLE 1\n"
    "Request: my cat as a medieval knight\n"
    "QUESTION: What does your cat look like?\n"
    "OPTIONS: Orange tabby | Black | Grey and white | Calico\n\n"
    "EXAMPLE 2\n"
    "Request: my cat as a medieval knight\n"
    "The user answered: Grey and white\n"
    "WIDTH: NONE\n"
    "HEIGHT: NONE\n"
    "NEGATIVE: NONE\n"
    "NOTE: I chose a realistic painted style and a castle courtyard.\n"
    "PROMPT: A detailed realistic oil painting of one grey and white cat dressed as a medieval knight, "
    "standing upright in a sunlit castle courtyard. The cat stands in the centre, facing slightly left, "
    "its white chest and paws showing beneath a polished steel breastplate engraved with a small silver "
    "crest, a deep crimson cape hanging from its shoulders. One front paw rests on the hilt of a short "
    "sword whose tip touches the worn grey flagstones. Behind it, pale sandstone walls rise to an oak "
    "gate under a hanging blue and gold banner. Warm late-afternoon sunlight falls from the right and "
    "catches the edges of the armour. The composition is centred and heroic, with a calm, proud mood.\n\n"
    "EXAMPLE 3\n"
    "Request: a wide banner of two robots, a big red one and a small blue one, playing chess in a park, "
    "no people\n"
    "WIDTH: 1664\n"
    "HEIGHT: 928\n"
    "NEGATIVE: people\n"
    "NOTE: I chose a bright 3D animated style.\n"
    "PROMPT: A bright 3D animated film still of exactly two robots playing chess at a stone table in a "
    "green city park on a summer afternoon. On the left sits a big boxy red robot with dented painted "
    "steel panels and round glowing amber eyes, one heavy hand hovering over a black knight. On the "
    "right sits a small round blue robot with a glossy enamel shell and a single thin antenna, leaning "
    "in to study the board. The chessboard between them is worn white marble with carved wooden pieces. "
    "Behind them, leafy oak trees and an empty gravel path fade into soft focus. Warm sunlight filters "
    "through the leaves and dapples the table. The composition is wide and balanced, with a friendly, "
    "playful mood.\n"
)


# How an edit instruction names the attached pictures. Qwen-Image 2.1's own
# text encoder labels them "<image1>", "<image2>", ... in upload order
# (ComfyUI comfy/text_encoders/qwen_image21.py, the tokenizer's template), and
# the engine's upstream enhancer writes that tag for 2+ pictures. The form is
# not yet checked against a real render: guides/picture/skills.md says so.
EDIT_REF = "<image%d>"
EDIT_PICTURES_LABEL = "Pictures to work from"   # the edit mode's ref_images label, below
EDIT_MAX_PICTURES = 10
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "one": 1, "two": 2,
             "three": 3, "four": 4, "five": 5}
_PICTURE_NUMBER_RE = re.compile(
    r"\b(?:(first|second|third|fourth|fifth)\s+(?:picture|image|photo|pic)"
    r"|(?:picture|image|photo|pic)\s*#?\s*(\d+|one|two|three|four|five))\b"
    r"|<image(\d+)>", re.IGNORECASE)
_BARE_REF_RE = re.compile(r"(?<!<)\b(?:picture|image)\s*(\d+)\b", re.IGNORECASE)


def _picture_numbers(text):
    """Every picture number a text names ("picture 2", "the second image", "<image3>")."""
    out = set()
    for m in _PICTURE_NUMBER_RE.finditer(text or ""):
        word = (m.group(1) or m.group(2) or m.group(3) or "").lower()
        n = _ORDINALS.get(word) or (int(word) if word.isdigit() else 0)
        if n:
            out.add(n)
    return out


def edit_missing(text, count):
    """The edit writer's picture check, run before the brain: the request
    (and its answers) against the pictures attached. -> a plain sentence
    naming the picture to add, or None when everything it names is there."""
    need = max([1] + sorted(_picture_numbers(text)))
    if count >= need:
        return None
    if count == 0 and need == 1:
        return "Add the picture to change under %s first, then ask again." % EDIT_PICTURES_LABEL
    if need > EDIT_MAX_PICTURES:
        return "An edit takes at most %d pictures, so there is no picture %d." % (EDIT_MAX_PICTURES, need)
    have = "none is" if count == 0 else ("only 1 is" if count == 1 else "only %d are" % count)
    return ("This names picture %d, and %s attached: add %s under %s, in the order you talk about them, "
            "then ask again." % (need, have, "it" if need - count == 1 else "the missing ones",
                                 EDIT_PICTURES_LABEL))


def edit_check(values, request):
    """The edit writer's check on its own drafts: pictures named in prose
    instead of the engine's own label, or a label past the pictures attached."""
    prompt = values.get("prompt") or ""
    problems = []
    bare = sorted({int(n) for n in _BARE_REF_RE.findall(prompt)})
    if bare:
        problems.append("The instruction names %s in words: call each picture by its label (%s)." % (
            ", ".join("picture %d" % n for n in bare), ", ".join(EDIT_REF % n for n in bare)))
    count = request.get("pictures")
    over = sorted(n for n in _picture_numbers(prompt) if isinstance(count, int) and n > count)
    if over:
        problems.append("The instruction names %s, but only %d picture%s attached." % (
            ", ".join(EDIT_REF % n for n in over), count, " is" if count == 1 else "s are"))
    return problems


EDIT_WRITER_PROMPT = (
    "You write the instruction for a picture EDITING model from a short request. The model gets the "
    "pictures the user attached, in order, and your instruction, and draws ONE new picture.\n"
    "The user calls the pictures picture 1, picture 2 and so on: the order they were added under "
    "\"" + EDIT_PICTURES_LABEL + "\". In the instruction, call them " + (EDIT_REF % 1) + ", "
    + (EDIT_REF % 2) + " and so on: that is how the model itself labels them. With only one picture, "
    "say \"the picture\".\n"
    "The new picture takes its size and shape from " + (EDIT_REF % 1) + ".\n"
    "The last line of the message says how many pictures are attached.\n\n"
    "Reply in plain text, no JSON, no markdown, no commentary, in EXACTLY one of these three shapes.\n\n"
    "To ask:\n"
    "QUESTION: <one short question>\n"
    "OPTIONS: <2 to 5 short choices separated by |>\n\n"
    "When the request needs a picture that is not attached:\n"
    "MISSING: <one sentence saying which picture to add and what it should show>\n\n"
    "To write:\n"
    "NEGATIVE: <NONE, unless the request names things to leave out: then just those things>\n"
    "NOTE: <which picture is the canvas when there are two or more, and anything else you chose>\n"
    "PROMPT: <the instruction, one paragraph>\n"
    "PROMPT comes last. Write nothing after it.\n\n"
    "RULES FOR THE INSTRUCTION\n"
    "1. Lead with the change, as an instruction: Replace, Put, Change, Make, Remove, Add.\n"
    "2. With two or more pictures, give each its role: which one is the CANVAS (its composition, "
    "background and everything not mentioned stay) and exactly what is taken from each other picture. "
    "Name every picture on its own, never \"both pictures\".\n"
    "3. Say exactly what changes. What stays is named by its role only (\"keep her face, pose and the "
    "background unchanged\"), never described again in detail.\n"
    "4. A face, a person or a product that must stay the same is pointed at (\"the woman from "
    + (EDIT_REF % 2) + "\"), never described feature by feature.\n"
    "5. A local change (one object, a colour, the background) stays short and exact. A new scene built "
    "from the pictures (the dog on a beach, a poster) gets its setting, light and composition described.\n"
    "6. Positive only: what to leave out goes in NEGATIVE. Only words that must appear as TEXT in the "
    "picture (a sign, a label) go in double quotes: this model draws quoted words as lettering. Keep "
    "every detail the user gave.\n"
    "7. Never name a picture number higher than the pictures attached: reply MISSING instead.\n\n"
    "WHEN TO ASK\n"
    "Ask ONE question, only when:\n"
    "a. CANVAS: two or more pictures are attached and the request does not say whose scene survives "
    "(\"swap their outfits\", \"combine these\"). \"Put X into picture 2\" already says picture 2.\n"
    "b. WHAT: the request does not say what to change.\n"
    "Once the user has answered, write.\n"
    "The room line in [brackets] is the form as it is now; it is not the request.\n\n"
    "EXAMPLE 1\n"
    "Request: put the lamp from picture 2 on the desk in picture 1\n"
    "[2 pictures attached.]\n"
    "NEGATIVE: NONE\n"
    "NOTE: " + (EDIT_REF % 1) + " is the canvas: its room and desk stay.\n"
    "PROMPT: Put the brass desk lamp from " + (EDIT_REF % 2) + " on the right side of the desk in "
    + (EDIT_REF % 1) + ", at a natural size for the desk, lit to match the room, and keep the desk, "
    "the room and everything else in " + (EDIT_REF % 1) + " unchanged.\n\n"
    "EXAMPLE 2\n"
    "Request: swap their outfits\n"
    "[2 pictures attached.]\n"
    "QUESTION: Which picture's person and scene should the result keep?\n"
    "OPTIONS: Picture 1 | Picture 2\n\n"
    "EXAMPLE 3\n"
    "Request: put the dog from picture 2 on the sofa in picture 1\n"
    "[1 picture attached.]\n"
    "MISSING: Add the picture of the dog as picture 2 under \"" + EDIT_PICTURES_LABEL + "\"; the sofa "
    "picture stays picture 1.\n\n"
    "EXAMPLE 4\n"
    "Request: make it night\n"
    "[1 picture attached.]\n"
    "NEGATIVE: NONE\n"
    "NOTE:\n"
    "PROMPT: Change the scene to night: a deep blue sky, warm light glowing in the windows and from "
    "the street lamps, and keep every building, object and the composition of the picture unchanged.\n"
)


ENGINE = {
    "id": "qwen-image",
    "cap": "image",
    "cap_word": "picture",
    "cap_order": 1,
    # server.py's api_generate has hand-tuned logic for t2i/edit (the 2.5
    # cfg default, the inertness note, ref-image wiring) that this pack's
    # generic field declarations do not fully capture -- keep it on the
    # legacy path rather than the generic one every other pack now uses.
    "legacy_dispatch": True,
    "roles": {
        "qwen_unet": ("unet", {"all": ["qwen_image"], "none": ["minimax", "vae"], "prefer": ["2.1"]}),
        "qwen_clip": ("clip", {"all": ["qwen3vl"], "none": ["minimax"], "prefer": ["8b"]}),
        "qwen_vae": ("vae", {"all": ["qwen_image", "vae"], "none": ["minimax"], "prefer": ["2.1"]}),
    },
    "primary": {"t2i": "qwen_unet", "edit": "qwen_unet"},
    # R3-1: mode-specific abilities, not the cap name itself. Before this,
    # the ability was literally "image" (== the cap), which was harmless
    # while qwen-image was the sole owner of cap "image" -- once a second
    # pack (cleanup) shares it via cap_from OR, that shared key meant
    # able["image"] could read True from cleanup's files alone, with t2i/
    # edit still unavailable and dying on a raw KeyError at dispatch. t2i
    # and edit both need all three roles, so both list the same three.
    "cap_from": ["t2i", "edit"],
    "provides": {
        "t2i": ["qwen_unet", "qwen_clip", "qwen_vae"],
        "edit": ["qwen_unet", "qwen_clip", "qwen_vae"],
    },
    "words": {
        "qwen_unet": "the Qwen-Image model",
        "qwen_clip": "its text encoder",
        "qwen_vae": "its image decoder",
    },
    "graphs": {
        "t2i": qwen_t2i_graph,
        "edit": qwen_edit_graph,
    },
    "describe": _describe,
    "mode_words": {
        "t2i": "A picture from a text prompt",
        "edit": "Edit a picture you upload",
    },
    "mode_rooms": {"t2i": "picture", "edit": "picture"},
    "mode_notes": {
        "t2i": "from words alone; has recipes for seamless tiles, set plates and characters",  # source: engines/qwen_image.py:200, :211, :218 (preset labels)
        "edit": "changes a picture you have; can also cut out a background with a clean edge, slower",  # source: engines/qwen_image.py:234 (remove-background preset note)
    },
    # L5: how a prompt must be written for each mode, drawn only from this
    # pack's own field hints/presets -- never a new claim.
    "prompt_guides": {
        "t2i": "Positive prompt only; put things to avoid in the separate "  # source: engines/qwen_image.py:141-149 (negative field hint)
               "\"Things to avoid\" field (it only takes effect once Guidance strength is at least 2.5). "
               "Return only the positive prompt. Do not write what to avoid.",
        "edit": "Describe the edit to make to the uploaded picture(s) as a positive "  # source: engines/qwen_image.py:234-236 (remove-background preset)
                "instruction, e.g. \"Remove the background\" works directly as a prompt.",
    },
    # P3d: the Picture guide's writing skills (engines/__init__.py "writers").
    "writers": {
        "t2i": {
            "label": "Picture prompt writer",
            "prompt": T2I_WRITER_PROMPT,
            "keys": {"WIDTH": "width", "HEIGHT": "height", "NEGATIVE": "negative", "PROMPT": "prompt"},
            "multiline": "PROMPT",
            "none_token": "NONE",
            "check": t2i_check,
            "make_time": False,
        },
        "edit": {
            "label": "Edit writer",
            "prompt": EDIT_WRITER_PROMPT,
            "keys": {"NEGATIVE": "negative", "PROMPT": "prompt"},
            "multiline": "PROMPT",
            "none_token": "NONE",
            "check": edit_check,
            "make_time": False,
            "pictures": "ref_images",
            "missing": edit_missing,
        },
    },
    # P3d: "Edit this result": a finished picture of either mode opens in edit.
    "edit_in": {"t2i": "edit", "edit": "edit"},
    # P2b: the Picture guide's "Not right? Tell the guide" skill
    # (engines/__init__.py's "revisers"); rules restated from
    # guides/picture/skills.md Skill 2 and knowledge.md's JUDGE/FIX section.
    "revisers": {
        "t2i": {
            "label": "Picture fixer",
            "prompt": PICTURE_REVISER_PROMPT,
            "keys": ["QUESTION", "DIAGNOSIS", "FIX", "PROMPT", "NOTE", "TWEAK"],
            "fills": "prompt",
            "edit_mode": "edit",
        },
    },
    # R4: describes the surface server.py's kind=="image" branch already
    # sends (lines ~1454-1486) -- defaults/ranges taken from there, not
    # invented. edit has no width/height fields: qwen_edit_graph takes its
    # latent size from the reference image via TextEncodeQwenImage21, so
    # those two kwargs are simply never read on that path.
    "fields": {
        "t2i": [
            {"id": "prompt", "label": "Prompt", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "width", "label": "Width", "type": "int", "default": 1328,
             "tier": "primary", "group": "Size", "order": 1,
             "units": "px", "range": [256, 2048], "ui_range": [768, 1536],
             "hint": "Snapped to a multiple of 16."},
            {"id": "height", "label": "Height", "type": "int", "default": 1328,
             "tier": "primary", "group": "Size", "order": 2,
             "units": "px", "range": [256, 2048], "ui_range": [768, 1536],
             "hint": "Snapped to a multiple of 16."},
            # Q21, MEASURED 2026-09-22: no visible effect at cfg 1 (pixel delta 0,
            # corr +1.000000), visible at cfg 2.5 (delta 34, corr +0.844) -- the
            # underlying mechanism is plain CFG needing cfg > 1 to do anything at
            # all, independent of Guidance style.
            {"id": "negative", "label": "Things to avoid", "type": "text", "default": "",
             "tier": "advanced", "group": "Content", "order": 2,
             "hint": "Only takes effect when Guidance strength is above 1."},
            {"id": "steps", "label": "Steps", "type": "int", "default": 20,
             "tier": "advanced", "group": "Quality", "order": 1,
             "units": "steps", "range": [1, 80], "ui_range": [15, 30]},
            # Q21, MEASURED 2026-09-23 (our internal CFG/APG/FreSca A/B notes):
            # plain CFG at 2.5 roughly halves brightness
            # on Qwen 2.1 (including a no-negative control); APG holds exposure --
            # this checkpoint's default guidance strength moved to 3 with Balanced.
            {"id": "cfg", "label": "Guidance strength", "type": "number", "default": 3.0,
             "tier": "advanced", "group": "Quality", "order": 2,
             "units": "CFG", "range": [1, 10], "ui_range": [1.5, 4],
             "hint": "Higher makes the picture follow your prompt more strictly. "
                     "\"Things to avoid\" needs more than 1 to work at all."},
            {"id": "resolution", "label": "Encoder resolution", "type": "int", "default": 1024,
             "tier": "advanced", "group": "Quality", "order": 3,
             "units": "px", "range": [512, 2048], "ui_range": [768, 1280],
             "hint": "Feeds TextEncodeQwenImage21's own resolution input."},
            {"id": "sampler", "label": "Sampling method", "type": "select", "default": "seeds_2",
             "options": ["seeds_2", "euler"],
             "tier": "advanced", "group": "Quality", "order": 4},
            {"id": "scheduler", "label": "Noise schedule", "type": "select", "default": "sgm_uniform",
             "options": ["sgm_uniform", "simple", "beta"],
             "tier": "advanced", "group": "Quality", "order": 5},
            # Q21: arm D of the A/B (our internal CFG/APG/FreSca A/B notes),
            # confirmed by the owner 2026-09-23.
            {"id": "guidance_style", "label": "Guidance style", "type": "select", "default": "Balanced",
             "options": ["Balanced", "Plain"],
             "tier": "advanced", "group": "Quality", "order": 6,
             "hint": "Balanced keeps pictures bright at higher guidance (APG + FreSca). "
                     "Plain is classic guidance, which darkens pictures once Guidance "
                     "strength reaches 2.5 or higher."},
            {"id": "apg_eta", "label": "APG eta", "type": "number", "default": 1.0,
             "tier": "advanced", "group": "Quality", "order": 7,
             "units": "eta", "range": [0.0, 2.0], "ui_range": [0.5, 1.5],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
            {"id": "apg_norm_threshold", "label": "APG norm threshold", "type": "number", "default": 10.0,
             "tier": "advanced", "group": "Quality", "order": 8,
             "units": "threshold", "range": [0.0, 50.0], "ui_range": [5.0, 20.0],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
            {"id": "apg_momentum", "label": "APG momentum", "type": "number", "default": 0.3,
             "tier": "advanced", "group": "Quality", "order": 9,
             "units": "momentum", "range": [0.0, 1.0], "ui_range": [0.0, 0.6],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
            {"id": "fresca_scale_low", "label": "FreSca scale (low freq)", "type": "number", "default": 1.0,
             "tier": "advanced", "group": "Quality", "order": 10,
             "units": "scale", "range": [0.0, 3.0], "ui_range": [0.5, 1.5],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
            {"id": "fresca_scale_high", "label": "FreSca scale (high freq)", "type": "number", "default": 2.0,
             "tier": "advanced", "group": "Quality", "order": 11,
             "units": "scale", "range": [0.0, 4.0], "ui_range": [1.0, 3.0],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
            {"id": "fresca_freq_cutoff", "label": "FreSca frequency cutoff", "type": "int", "default": 8,
             "tier": "advanced", "group": "Quality", "order": 12,
             "units": "cutoff", "range": [1, 64], "ui_range": [4, 16],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
        ],
        "edit": [
            {"id": "prompt", "label": "Prompt", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "ref_images", "label": "Pictures to work from", "type": "image_list",
             "tier": "primary", "group": "Content", "order": 2, "max": 10,
             "hint": "Up to 10. At least one is required."},
            # TextEncodeQwenImage21's own latent carries the output size here,
            # not width/height -- the hint says this without naming the node.
            {"id": "resolution", "label": "Encoder resolution", "type": "int", "default": 1024,
             "tier": "primary", "group": "Quality", "order": 1,
             "units": "px", "range": [512, 2048], "ui_range": [768, 1280],
             "hint": "Governs the output size for an edit. There is no separate "
                     "width or height field; the size comes from the picture you uploaded."},
            {"id": "negative", "label": "Things to avoid", "type": "text", "default": "",
             "tier": "advanced", "group": "Content", "order": 3,
             "hint": "Only takes effect when Guidance strength is above 1."},
            {"id": "steps", "label": "Steps", "type": "int", "default": 20,
             "tier": "advanced", "group": "Quality", "order": 2,
             "units": "steps", "range": [1, 80], "ui_range": [15, 30]},
            {"id": "cfg", "label": "Guidance strength", "type": "number", "default": 2.5,
             "tier": "advanced", "group": "Quality", "order": 3,
             "units": "CFG", "range": [1, 10], "ui_range": [1.5, 4]},
            {"id": "sampler", "label": "Sampling method", "type": "select", "default": "euler",
             "options": ["seeds_2", "euler"],
             "tier": "advanced", "group": "Quality", "order": 4},
            {"id": "scheduler", "label": "Noise schedule", "type": "select", "default": "simple",
             "options": ["sgm_uniform", "simple", "beta"],
             "tier": "advanced", "group": "Quality", "order": 5},
            # Q21: edit's own Default stays Plain -- arm D (t2i) was never
            # measured on edit; switching edit's default is an open owner
            # question. The field exists so a caller CAN ask for Balanced.
            {"id": "guidance_style", "label": "Guidance style", "type": "select", "default": "Plain",
             "options": ["Balanced", "Plain"],
             "tier": "advanced", "group": "Quality", "order": 6,
             "hint": "Balanced keeps pictures bright at higher guidance (APG + FreSca). "
                     "Plain is classic guidance, which darkens pictures once Guidance "
                     "strength reaches 2.5 or higher."},
            {"id": "apg_eta", "label": "APG eta", "type": "number", "default": 1.0,
             "tier": "advanced", "group": "Quality", "order": 7,
             "units": "eta", "range": [0.0, 2.0], "ui_range": [0.5, 1.5],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
            {"id": "apg_norm_threshold", "label": "APG norm threshold", "type": "number", "default": 10.0,
             "tier": "advanced", "group": "Quality", "order": 8,
             "units": "threshold", "range": [0.0, 50.0], "ui_range": [5.0, 20.0],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
            {"id": "apg_momentum", "label": "APG momentum", "type": "number", "default": 0.3,
             "tier": "advanced", "group": "Quality", "order": 9,
             "units": "momentum", "range": [0.0, 1.0], "ui_range": [0.0, 0.6],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
            {"id": "fresca_scale_low", "label": "FreSca scale (low freq)", "type": "number", "default": 1.0,
             "tier": "advanced", "group": "Quality", "order": 10,
             "units": "scale", "range": [0.0, 3.0], "ui_range": [0.5, 1.5],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
            {"id": "fresca_scale_high", "label": "FreSca scale (high freq)", "type": "number", "default": 2.0,
             "tier": "advanced", "group": "Quality", "order": 11,
             "units": "scale", "range": [0.0, 4.0], "ui_range": [1.0, 3.0],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
            {"id": "fresca_freq_cutoff", "label": "FreSca frequency cutoff", "type": "int", "default": 8,
             "tier": "advanced", "group": "Quality", "order": 12,
             "units": "cutoff", "range": [1, 64], "ui_range": [4, 16],
             "enabled_when": {"field": "guidance_style", "equals": "Balanced"},
             "disabled_reason": "Only used when Guidance style is Balanced."},
        ],
    },
    # R3: named parameter sets. `default` covers the plain path (the app's
    # existing production defaults); the others cite real measurements --
    # see engines/audio.py's presets for the same discipline.
    "presets": {
        "t2i": [
            # Q21: arm D of the A/B, owner-confirmed 2026-09-23 (internal
            # research notes, measured on our hardware). Costs about 3x the time
            # of cfg 1 (76s vs 26s at 1024^2 on the 5080), and it can add props
            # the prompt didn't ask for (an umbrella appeared in 4 of 6 test
            # renders).
            {"id": "default", "label": "Default", "note":
             "Balanced guidance (APG + FreSca), Guidance strength 3. Holds exposure "
             "where plain guidance darkens pictures at this strength. Costs about "
             "3x the time of Guidance strength 1 (76s vs 26s at 1024^2 on the "
             "5080), and it can add props the prompt didn't ask for (an umbrella "
             "appeared in 4 of 6 test renders). Measured 2026-09-23.",
             "values": {"width": 1328, "height": 1328, "steps": 20, "cfg": 3.0,
                        "sampler": "seeds_2", "scheduler": "sgm_uniform", "guidance_style": "Balanced"}},
            # Q21: about 3x faster than Default. At cfg 1 "Things to avoid" has no
            # effect (measured: max pixel delta 0) -- listed here so that limit is
            # visible rather than a silent surprise.
            {"id": "fast-plain", "label": "Fast / plain", "note":
             "About 3x faster than Default. At Guidance strength 1, \"Things to "
             "avoid\" has no effect (measured: max pixel delta 0). "
             "Measured 2026-09-23.",
             "values": {"cfg": 1.0, "sampler": "euler", "scheduler": "simple",
                        "steps": 25, "guidance_style": "Plain"}},
            # Full citation: 25 steps @ 1024^2 beats the vendor's own 40/2048 default
            # for tiling work -- 40 steps makes the seam gradient WORSE 6/6 paired
            # (+50% time), and both axes show a systematic warm colour drift 12/12.
            # Measured 2026-09-21 (our internal 40-step/2048px vendor-default
            # test notes). Q21: arm D
            # (Balanced) worsened seams 3/3, 2-4x -- no guided arm beat cfg 1 on
            # seams, so this preset stays plain. Measured 2026-09-23.
            {"id": "seamless-tile", "label": "Seamless tile", "note":
             "25 steps at a smaller size beats the higher-step, higher-resolution "
             "default for tiling work: more steps made the seam worse and drifted "
             "the colour. Measured 2026-09-21. Balanced guidance worsened seams "
             "3/3, at 2-4x the time; stays plain. Measured 2026-09-23.",
             "values": {"steps": 25, "width": 1024, "height": 1024, "resolution": 1024,
                        "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "guidance_style": "Plain"}},
            # Full citation: for a reference plate reused across multiple video
            # shots, a plate WITH people gets duplicated by the video model when it
            # is later used as a multi-shot reference (a symmetric plate made this
            # worse). Prefer an asymmetric angle over a head-on view for the same
            # reason. Documented, not benchmarked:
            # internal measurement notes.
            {"id": "set-plate", "label": "Set plate (no people)", "ref_role": "set", "note":
             "For a reference plate reused across multiple video shots, one with "
             "people gets duplicated when it is later used as a video reference. "
             "Prefer an asymmetric angle over a head-on view for the same reason.",
             "values": {"negative": "people, person, human figures, characters"}},
            # Full citation: documented, not benchmarked,
            # internal measurement notes.
            {"id": "character-anchor", "label": "Character anchor (neutral light)", "ref_role": "character", "note":
             "Character reference photos must NOT be lit in the film's own scheme, "
             "or that lighting carries into every shot and fights the set.",
             "values": {"negative": "dramatic lighting, colored lighting, mood lighting"}},
        ],
        "edit": [
            # Q21: stays exactly today's values -- arm D (t2i) was never measured
            # on edit; switching edit's own default to Balanced is an open owner
            # question. guidance_style/sampler/scheduler default to Plain/euler/
            # simple (see the fields above), so this preset is unchanged.
            {"id": "default", "label": "Default", "note":
             "Same guidance mechanism as the picture mode. 2.5 measured live, "
             "no effect below that.",
             "values": {"resolution": 1024, "steps": 20, "cfg": 2.5}},
            # R3: background removal on this same picture model, as a preset
            # (not a separate pack). 50s, clean matte, 30.2% fully transparent.
            # ~10x slower than the dedicated BiRefNet tool (cutout mode,
            # engines/cleanup.py) -- use that for bulk. Measured 2026-09-21,
            # our internal capability notes.
            {"id": "remove-background", "label": "Remove background", "note":
             "50s, clean matte. The dedicated background removal tool is "
             "about 10x faster and better for bulk work. Measured 2026-09-21.",
             "values": {"prompt": "Remove the background, and output a PNG image"}},
        ],
    },
    # R2: ported verbatim from the old page's IMG_Q / IMG_Q_WHY constants
    # (index.html, since replaced) -- same four steps counts, same "why"
    # text, Standard still the default. No new tiers invented.
    "quality": {
        "t2i": [
            {"id": "draft", "label": "Draft", "why": "fastest, a bit rough",
             "values": {"steps": 12}},
            {"id": "standard", "label": "Standard", "why": "best balance",
             "values": {"steps": 20}, "default": True},
            {"id": "high", "label": "High", "why": "more detail",
             "values": {"steps": 30}},
            {"id": "max", "label": "Max", "why": "slowest, most detail",
             "values": {"steps": 40}},
        ],
        "edit": [
            {"id": "draft", "label": "Draft", "why": "fastest, a bit rough",
             "values": {"steps": 12}},
            {"id": "standard", "label": "Standard", "why": "best balance",
             "values": {"steps": 20}, "default": True},
            {"id": "high", "label": "High", "why": "more detail",
             "values": {"steps": 30}},
            {"id": "max", "label": "Max", "why": "slowest, most detail",
             "values": {"steps": 40}},
        ],
    },
    # H2: "Try this" -- t2i needs nothing but words; edit needs a picture to
    # work from first.
    "examples": {
        "t2i": [
            {"id": "t2i-try", "label": "A picture from words", "recipe": "default",
             "quality": "standard",
             "values": {"prompt": "a cozy cabin in a snowy forest at sunset"},
             "why": "a picture from words alone", "needs": None},
        ],
        "edit": [
            {"id": "edit-try", "label": "Edit a picture", "recipe": "default",
             "quality": "standard",
             "values": {"prompt": "add a warm golden glow to the light"},
             "why": "changes a picture you upload", "needs": "picture"},
        ],
    },
    "licence": {
        "name": "Qwen Research License",
        "shippable": False,
        "attribution": "Qwen-Image 2.1 by Alibaba Qwen team",
    },
}
