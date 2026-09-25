"""Engine-pack loader for the ComfyUI server.

This module is the seam between the (model-agnostic) core in server.py and
concrete diffusion models. The core never knows a model name; everything
model-specific lives in *packs*.

Pack contract
-------------
A pack is any sibling module in this package (i.e. a ``.py`` file next to
this file, or a subpackage) that exposes a module-level dict named
``ENGINE``. A module without a dict ``ENGINE`` — or one whose ``id`` is
falsy — is ignored. The dict has exactly these keys:

  id        str                       unique pack id, e.g. "qwen-image"
  cap       str                       lane capability, e.g. "image" or "video"
  roles     dict  role_name -> (pool_name, rule_dict)
                       server-side discovery: which pool to scan and with
                       what rule (all / none / any name fragments, prefer)
  provides  dict  ability_name -> list[role_name]
                       an ability is satisfied iff EVERY listed role
                       resolved to a model in the models dict
  words     dict  role_name -> str
                       plain-English label used in "missing X" messages
  graphs    dict  mode_name -> callable(args, models) -> graph_dict
                       builds a ComfyUI graph dict for one run mode
  describe  callable(models) -> str
                       short human label, e.g. "Qwen-Image 2.1 (GGUF)"
  licence   dict (or list of dicts, one per regime) with keys: name (str),
                       shippable (bool), attribution (str), and optionally
                       "modes" (list[str]) naming which modes a list entry
                       covers -- absent means the whole pack
  cap_word  optional str, plain-English noun for the cap in a sentence
                       ("picture" for "image"); absent uses the cap name.
                       Contract: a BARE noun, no article or quantifier --
                       it must fit "the ___ models" and similar templates,
                       so "sound" not "some music".
  cap_order optional int, display order for the cap (e.g. tabs/buttons on
                       the page) -- lower sorts first. Absent means 100, so
                       an undeclared cap sorts after every declared one
                       instead of colliding at 0.
  mode_words  optional dict  mode_name -> short human label for that mode,
                       e.g. {"song": "A song with words"}. Absent means the
                       UI falls back to the bare mode name.
  mode_rooms  optional dict  mode_name -> room id, e.g. {"song": "music"}.
                       Which task room (rooms.json at the repo root) the mode
                       is offered in. The core never maps mode -> room itself.
                       A mode with no entry, or naming a room rooms.json does
                       not list, still shows up: rooms() builds a room for it
                       from the mode's own label, in its cap's group.
  mode_notes  optional dict  mode_name -> one plain line saying WHEN to pick
                       this mode over its room-mates, e.g. {"cutout": "about
                       10x faster ..., meant for bulk"}. Drawn only from
                       evidence already in the pack (preset notes, quality
                       `why` text, comments) or the internal component-inventory
                       notes, cited with a `# source:` comment -- never
                       a new claim. No evidence means no entry; the UI then
                       shows nothing rather than an invented reason.
  prompt_guides  optional dict  mode_name -> a short instruction telling the
                       L5 prompt helper how a prompt for THIS mode must be
                       written (e.g. the positive-only rule, a tag style for
                       songs, the shot format for video). Same discipline as
                       mode_notes: drawn ONLY from what the pack already
                       documents (field hints, preset notes, docstrings) or
                       the repo's own notes, cited with a `# source:` comment.
                       A mode with no entry falls back to a generic
                       instruction -- the core, not the pack, owns that
                       fallback text.
  writers   optional dict  mode_name -> a writing skill for the room's guide
                       (POST /api/guide/skill): a topic in, this mode's field
                       values out. Each is a dict with:
                         label       short name, e.g. "Song writer"
                         prompt      the writer's system prompt, this
                                     engine's own writing rules
                         keys        {OUTPUT_KEY: field id}, e.g. {"TAGS":
                                     "tags"}: the reply is one "KEY: value"
                                     line per key, never JSON (small models do
                                     not reliably escape newlines in a JSON
                                     string)
                         multiline   the one key (also in `keys`) whose value
                                     runs from its line to the end, e.g.
                                     "LYRICS"; or a list of such keys in
                                     reply order, e.g. ["CAPTION",
                                     "LYRICS"]: then each runs until the
                                     next line that starts with one of the
                                     writer's keys, and the first is required
                         none_token  the word that means "empty", e.g. "NONE".
                                     On a multiline key it means an empty
                                     field; on any other key, "leave the
                                     field as it is"
                         keep_token  optional word that means "leave the
                                     field as it is" on a multiline key,
                                     e.g. "KEEP"
                         needs       optional {field id: plain sentence}: a
                                     field (an upload) the room must already
                                     hold, per the request's context, before
                                     the writer can write; without it the
                                     sentence is the answer and the brain is
                                     not asked
                         max_tokens  optional reply budget (default 1024)
                         options     optional {field id: [allowed values]} for
                                     a text field the engine only accepts from
                                     a fixed list (matched without case)
                         derive      optional callable(values, request) ->
                                     {field id: value} for fields the pack
                                     computes (e.g. a clip's length from its
                                     spoken line, or from a duration the
                                     user stated in the request). These
                                     win over the reply. `values` and
                                     `request` are as check's below.
                         check       callable(values, request) -> [plain
                                     problem sentences]; [] means fine.
                                     `values` are the mode's coerced field
                                     values. It also runs at Make time: a
                                     request with problems is refused with
                                     needs_confirm until the user confirms.
                         make_time   optional bool, default True: False
                                     keeps `check` to the writer's own
                                     drafts, never a Make-time confirm
                         target      optional {"cap", "mode"}: the words are
                                     for ANOTHER mode (in another room),
                                     e.g. a 3D mode's source picture; `keys`
                                     then name THAT mode's field ids, and
                                     the page switches there on "Use these"
                         topic_label optional: the topic box's placeholder
                                     for a mode that has no prompt field
                         pictures    optional field id of the mode's picture
                                     list the writer writes about: the page
                                     sends those pictures with the topic
                         missing     optional callable(text, count) -> a
                                     plain sentence naming the picture still
                                     to add, or None: `text` is the request
                                     and answers, `count` the pictures
                                     attached; checked before the brain
                       A reply may instead be one "QUESTION: ..." line: the
                       writer asks the user one thing before writing,
                       optionally followed by "OPTIONS: a | b | c" (2-5 short
                       choices the page offers as buttons). A writer with
                       `pictures` may instead reply one "MISSING: ..." line:
                       the picture to add, returned as a sentence, never a
                       question. A written reply may carry one "NOTE: ..." line (e.g. the defaults the
                       writer chose); a NOTE line inside a multiline part
                       ends it.
  revisers  optional dict  mode_name -> a fixing skill for the room's guide
                       (POST /api/guide/revise, "Not right? Tell the
                       guide"): a finished result, the prompt that made it
                       and the user's complaint in; what went wrong and a
                       revised prompt out. Each is a dict with:
                         label       short name, e.g. "Picture fixer"
                         prompt      the reviser's system prompt: this
                                     engine's own known failure modes
                         keys        the reply's line keys, in order:
                                     QUESTION, DIAGNOSIS, FIX, PROMPT, NOTE,
                                     TWEAK, each "KEY: value" on ONE line
                         fills       the field id PROMPT fills on a reroll,
                                     e.g. "prompt"
                         edit_mode   the mode (same cap) to point at when
                                     FIX is "edit", e.g. "edit"; None when
                                     the mode has nothing to edit with
                         fixes       optional: the FIX values this reviser
                                     may give, replacing the default
                                     ["edit", "reroll"]. Either a list of
                                     those words (e.g. ["reroll"] for a
                                     clip, which is never edited in place),
                                     or {FIX word: spec} for fixes that land
                                     elsewhere. A spec may carry "target"
                                     {"cap", "mode"} (where the fix lands:
                                     another room's mode, or this job's
                                     own), "fills" (the field PROMPT fills
                                     there: PROMPT is then required) and
                                     "settings": True (a SETTINGS line,
                                     "field id = value; ...", sets the
                                     target's fields). A spec with none of
                                     these is advice only.
                       A non-blank QUESTION ends the reply; otherwise FIX
                       must be one of `fixes`, and PROMPT non-blank for a
                       listed word or a spec that fills a field.
  edit_in   optional dict  mode_name -> a mode of the same cap that edits a
                       finished result of this one ("Edit this result"):
                       its first picture field gets the result.
  fields    optional dict  mode_name -> list of field-descriptor dicts, each
                       with "id", "label", "type" (text/textarea/number/int/
                       select/checkbox/audio/image/image_list/video_list) and
                       optionally "default", "hint", "options" (for select).
                       "number" coerces to float, "int" to int -- use "int"
                       whenever the builder's own kwarg is typed int (e.g.
                       bpm), or a float default will silently drift the graph
                       from the frozen source. This is how a pack whose modes
                       take wildly different inputs describes its own form --
                       the core renders whatever it is told, never a
                       hardcoded field list per mode.

                       A field may also carry curation, not just content
                       (Unreal UPROPERTY vocabulary -- the host renders, the
                       pack only declares):
                         tier      "primary" or "advanced" -- primary is the
                                   few fields touched every time; be ruthless,
                                   more than ~5 primary fields in one mode
                                   means it wasn't curated.
                         group     an authored group name; order (int) is
                                   this field's position within that group.
                                   Never rely on dict order.
                         units     e.g. "BPM", "seconds", "px", "steps" --
                                   required on every number/int field.
                         range     [hard_min, hard_max] -- a VALID clamp.
                         ui_range  [lo, hi] -- what a slider should COVER,
                                   the comfortable span. Different from
                                   `range` on purpose (e.g. bpm is valid
                                   40-220 but comfortable 70-160); must lie
                                   inside `range`.
                         enabled_when / disabled_reason
                                   a tiny DECLARATION the core evaluates,
                                   never a string it eval()s: e.g.
                                   {"field": "plan", "equals": True}.
                                   Supports equals / not_equals / truthy.
                                   `disabled_reason` is a plain sentence
                                   saying why the field is off when the
                                   condition fails -- the point is the
                                   reason, not the toggle.
                         max       optional int, on an "image_list" field:
                                   the real limit the graph honours (the
                                   builder's own `[:N]` slice). The core
                                   refuses rather than silently truncating
                                   once a caller would pass more than this.

  presets   optional dict  mode_name -> list of {"id", "label", "note",
                       "values"} dicts. A named parameter set (Krita/Fooocus
                       pole): `values` is a partial field map: mode; the
                       preset absorbs complexity so the visible form only
                       shows the per-generation delta. `note` is a one-line
                       citation of where the setting was measured -- presets
                       encode a measurement, not a guess.
                       A preset may also carry "ref_role": "set" | "character"
                       -- what kind of reference a picture made with this
                       preset is meant to be. The core only ever compares
                       this against a reference's own recorded role; it never
                       compares preset names.
  mode_deps optional dict  mode_name -> callable() -> str or None. A check
                       the pack runs for itself, independent of any lane's
                       model files -- e.g. a local Python package the CORE
                       needs for a `post` step (numpy/Pillow for Pixel Art's
                       quantise). Returns a plain sentence naming the fix
                       when the mode cannot run, or None when it can. Folded
                       into the same `available`/`missing` the UI already
                       shows for a missing model (portability fix P1) --
                       this is not a second, separate "can't run" concept.
  post      optional dict  mode_name -> callable(input_bytes, filename, args)
                       -> (output_bytes, output_filename). A step the CORE
                       runs after a lane finishes rendering this mode: pure
                       Python (stdlib + Pillow only, no ComfyUI node, no
                       second model call) applied to the lane's primary
                       output. The core fetches the render, calls this, and
                       keeps the result under data/outputs/<job_id>/ as an
                       ADDITIONAL output of type "local" -- the lane's own
                       render is never discarded. A mode without "post" is
                       untouched; this is how a pack that needs a step AFTER
                       ComfyUI (e.g. Pixel Art's deterministic quantise) says
                       so without the core knowing what that step does.
                       That output carries "post": true and is the job's
                       RESULT: the page shows it first and offers the
                       lane's render as the view from before the step.
  post_words optional dict  mode_name -> what the page calls that step,
                       e.g. {"pixelart": "the pixel step"}. Absent means
                       "the finishing step".
  examples  optional dict  mode_name -> list of {"id", "label", "recipe",
                       "quality", "values", "why", "needs"} dicts -- the
                       "Try this" row (H2). `recipe` is a preset id from this
                       mode's own `presets` or None; `quality` is a tier id
                       from this mode's own `quality` or None; `values` is a
                       partial field map, same shape as a preset's, and may
                       only use this mode's declared field ids; `why` is one
                       plain line saying what the example shows, in terms of
                       the result (never a step count); `needs` is None or a
                       cap word (see `cap_word` above) naming an input the
                       user must still supply -- e.g. a mode whose main input
                       is a picture the user uploads sets `needs: "picture"`,
                       leaving the example to fill everything else. Every
                       mode should carry at least one example.
  quality   optional dict  mode_name -> list of tier dicts, each with:
                       "id", "label", "why" (one plain sentence, never a
                       step count or model name), "values" (a partial field
                       map, same shape as a preset's), "default" (bool,
                       exactly one True per mode), and optionally "requires"
                       (an ability name from this pack's `provides`) with
                       "unavailable_reason" (a plain sentence) when it does.
                       This is the "quick or high end" ladder the page picks
                       from -- the numbers behind a label live here so the
                       page never needs to know a step count exists.

The public API of this module (packs, role_pool, role_rules, model_keys,
abilities, missing_words, mode_ability, describe, graph_for, modes_for,
licences, licence_for, caps, cap_word, cap_order, mode_words, mode_room,
mode_note, prompt_guide, writer, parse_writer_reply, reviser, parse_reviser_reply, reviser_fixes, edit_in, rooms, fields, presets, quality, examples, post_for,
unet_loader, quant_words) is all the core needs to stay model-agnostic.
"""

import importlib
import json
import logging
import os
import pkgutil

# Cache so the directory scan happens once per process.
_PACKS = None
# pack id -> the pack module's file, so a process lane can pass its run plan
# the pack's own asset directory ({pack} in argv).
_PACK_FILES = {}


def _discover():
    """Scan this package's directory once and return its ENGINE dicts."""
    global _PACKS
    if _PACKS is None:
        found = []
        pkg = __package__ or "engines"
        here = os.path.dirname(__file__)
        for mod in pkgutil.iter_modules([here]):
            if mod.name.startswith("_"):
                continue
            module = importlib.import_module(f"{pkg}.{mod.name}")
            engine = getattr(module, "ENGINE", None)
            if isinstance(engine, dict) and engine.get("id"):
                found.append(engine)
                _PACK_FILES[engine["id"]] = os.path.abspath(
                    os.path.join(here, mod.name + ".py"))
        found.sort(key=lambda e: e["id"])
        _PACKS = found
    return _PACKS


def packs():
    """All discovered ENGINE dicts, sorted by id."""
    return _discover()


def role_pool():
    """role -> pool_name, merged across packs."""
    out = {}
    for pack in _discover():
        for role, (pool, _rule) in (pack.get("roles") or {}).items():
            out[role] = pool
    return out


def role_rules():
    """role -> rule_dict, merged across packs."""
    out = {}
    for pack in _discover():
        for role, (_pool, rule) in (pack.get("roles") or {}).items():
            out[role] = rule
    return out


def model_keys():
    """Every role name, as a tuple."""
    return tuple(role_pool())


def abilities(models):
    """What a lane can do, from the files it actually has.

    A role entry may be a LIST, meaning "any one of these is enough" -- H3 accepts
    either its nvfp4 or its int8 text encoder, and requiring one specific file told
    a perfectly capable lane it could not do video.

    A pack may set "cap_from" to name which of its abilities make its capability
    true. Without it every ability counts, which wrongly let a turbo LoRA alone
    report the lane as video-capable.
    """
    def resolved(entry):
        if isinstance(entry, (list, tuple)):
            return any(models.get(r) for r in entry)   # any-of
        return bool(models.get(entry))

    # Two passes, not one -- D2's ordering discipline applies here too. A
    # pack's own ability may be named the SAME as its cap (qwen-image's
    # single ability is literally "image", by design -- see mode_ability()'s
    # F1 comment). With only one pack per cap that self-reference was
    # harmless; with two packs sharing a cap (R1), a single combined loop
    # let whichever pack's cap-line ran LAST clobber the other's ability
    # value under that shared key, so the cap's truth silently depended on
    # pack discovery order. Computing every ability first, then every cap's
    # OR strictly from those already-settled ability values, makes the
    # result the same regardless of which pack is processed first.
    out = {}
    packs = _discover()
    for pack in packs:
        for ability, roles in pack["provides"].items():
            out[ability] = all(resolved(r) for r in roles)
    cap_true = {}
    for pack in packs:
        counts = pack.get("cap_from") or list(pack["provides"])
        cap = pack["cap"]
        cap_true[cap] = cap_true.get(cap, False) or any(out.get(a) for a in counts)
    out.update(cap_true)
    return out


def missing_words(models, ability):
    """Plain-English words for the roles still missing for an ability.

    An any-of group contributes ONE label (they describe the same thing, e.g. two
    quantisations of the same encoder) and only when none of the group resolved.

    If `ability` is not itself a key in any pack's `provides` -- e.g. a CAP name
    like "audio" or "video", which is not one of its own pack's abilities when a
    pack bundles several modes under one cap -- fall back to the SHORTEST path to
    satisfying that cap: among ALL packs sharing it, whichever `cap_from` ability
    (of whichever pack) has the fewest roles still missing, ties broken by
    `cap_order` then pack id so the answer is deterministic. `abilities()` makes
    a cap true if ANY pack's `cap_from` ability is true, so this is the "you are
    N files away from one working engine" mirror of that OR -- the union of
    every ability's missing roles would tell the user nothing actionable.

    A cap may be shared by a GENERATOR pack and a TOOL pack that merely also
    satisfies it (R1 -- e.g. a background-removal pack under "image", alongside
    the picture generator). A tool pack is very often "shortest" by file count
    (one loader vs a generator's several), so an unrestricted scan would answer
    "what do I need for pictures?" with "install a background remover" -- true
    by file count, useless in practice, since that pack cannot make a picture
    at all. So the scan is restricted to the DECLARING packs for this cap --
    those with something to say about its identity (`cap_word` or `cap_order`,
    the same signal D2 uses to pick a cap's headline) -- and only falls back to
    every pack sharing the cap when NONE of them declares either (a cap made
    entirely of tools, with no primary engine to prefer).
    """
    def words_for(pack, roles):
        out = []
        for entry in roles:
            if isinstance(entry, (list, tuple)):
                if not any(models.get(r) for r in entry):
                    w = (pack.get("words") or {}).get(entry[0], entry[0])
                    if w not in out:
                        out.append(w)
            elif not models.get(entry):
                out.append((pack.get("words") or {}).get(entry, entry))
        return out

    for pack in _discover():
        roles = pack["provides"].get(ability)
        if roles is None:
            continue
        return words_for(pack, roles)

    sharing = [pack for pack in _discover() if pack["cap"] == ability]
    declaring = [pack for pack in sharing if pack.get("cap_word") or "cap_order" in pack]
    pool = declaring or sharing

    def tie_key(pack):
        return (pack.get("cap_order", 100), pack["id"])

    best, best_pack = None, None
    for pack in pool:
        candidates = pack.get("cap_from") or list(pack["provides"])
        pack_best = None
        for a in candidates:
            w = words_for(pack, pack["provides"].get(a) or [])
            if pack_best is None or len(w) < len(pack_best):
                pack_best = w
        if pack_best is None:
            continue
        if best is None or len(pack_best) < len(best) or (
                len(pack_best) == len(best) and tie_key(pack) < tie_key(best_pack)):
            best, best_pack = pack_best, pack
    return best or []


def mode_ability(cap, mode):
    """The ability name that decides whether `mode` is available -- the ONE
    resolution `available` and `missing_words` must both use, so they can
    never disagree (the F1 bug: a mode that is not itself an ability, e.g.
    image's "t2i"/"edit" which share the single "image" ability, always
    read as unavailable with an empty `missing` -- a silent false negative).

    If `mode` is itself a declared ability of its owning pack (video/audio
    modes: fl2va, ref2v, song, music, ... -- these double as both mode and
    ability name) it is used directly. Otherwise, fall back to the pack's
    cap, the same name `missing_words` resolves a cap-shaped ability to via
    its own `cap_from` search, and `abilities()` sets true from ANY
    `cap_from` ability. First pack providing cap+mode wins, matching
    fields()/presets()/quality()'s own precedence.
    """
    for pack in _discover():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return mode if mode in pack["provides"] else cap
    return mode


def describe(models, cap):
    """First non-empty describe() from a pack with ``cap``; "" if none.

    A pack whose describe() raises falls back to its id.
    """
    for pack in _discover():
        if pack["cap"] != cap:
            continue
        try:
            text = pack["describe"](models)
        except Exception:
            text = pack["id"]
        if text:
            return text
    return ""


def legacy_dispatch(cap, mode):
    """Whether server.py's own hand-tuned image/video logic (cfg defaults,
    frame-grid snapping, turbo derivation) must handle this (cap, mode),
    rather than the generic field-driven path every other pack uses.

    True only when the pack that owns this cap+mode declares
    ``"legacy_dispatch": True`` (qwen-image and minimax-h3 today, for logic
    this rewrite must not touch). An unowned (cap, mode) is never legacy --
    the core must refuse it, not guess a fallback mode.
    """
    for pack in _discover():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return bool(pack.get("legacy_dispatch"))
    return False


def has_legacy(cap):
    """Whether ANY pack with this cap declares "legacy_dispatch" -- used to
    decide how an unrecognised/blank mode string should be treated (the
    legacy packs' own "no mode given" default, vs. a clean refusal for a cap
    with no legacy pack at all)."""
    return any(pack["cap"] == cap and pack.get("legacy_dispatch") for pack in _discover())


def graph_for(cap, mode, args, models):
    """Build the graph via the first pack providing ``cap`` + ``mode``.

    Raises LookupError if no pack provides that cap+mode.
    """
    for pack in _discover():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return pack["graphs"][mode](args, models)
    raise LookupError(f"no engine pack for cap={cap!r} mode={mode!r}")


def modes_for(cap):
    """Mode names across all packs with ``cap`` (packs in id order)."""
    modes = []
    for pack in _discover():
        if pack["cap"] == cap:
            modes.extend(pack["graphs"])
    return modes


def licences():
    """Each pack's licence entries, each with an added 'engine' key = pack id.

    A pack's ``licence`` may be ONE dict (a single regime for the whole pack,
    as every pre-audio pack declares) or a LIST of dicts (several regimes
    under one pack -- audio carries four). A list entry may name which modes
    it covers via a ``modes`` key; one without it covers the whole pack.
    """
    out = []
    for pack in _discover():
        lic = pack.get("licence")
        if not lic:
            continue
        entries = lic if isinstance(lic, list) else [lic]
        for entry in entries:
            entry = dict(entry)
            entry["engine"] = pack["id"]
            out.append(entry)
    return out


def caps():
    """Every pack's cap, sorted -- the full set of media types this app knows."""
    return sorted({pack["cap"] for pack in _discover()})


def cap_word(cap):
    """Plain-English noun for a cap, e.g. "picture" for "image".

    A pack declares "cap_word"; the FIRST pack that actually DECLARES one
    for this cap wins -- not merely the first pack with the cap. Two packs
    may share a cap (e.g. qwen-image and a cleanup pack both under "image");
    a pack with nothing to say about the word must never shadow one that
    does, or correctness would depend on alphabetical pack id order. Falls
    back to the cap name itself if no pack for this cap declares a word.
    Contract: a BARE noun, no article or quantifier -- callers plug it into
    "the ___ models" and similar templates, so "sound" is correct and "some
    music" is not.
    """
    for pack in _discover():
        if pack["cap"] == cap and pack.get("cap_word"):
            return pack["cap_word"]
    return cap


def cap_order(cap):
    """Display order for a cap, e.g. for tabs/buttons -- an int, lower first.

    A pack declares "cap_order"; the FIRST pack that actually DECLARES one
    for this cap wins, same reasoning as cap_word() above. Falls back to
    100 if no pack for this cap declares an order, so an undeclared cap
    sorts after every declared one rather than colliding at 0.
    """
    for pack in _discover():
        if pack["cap"] == cap and "cap_order" in pack:
            return pack["cap_order"]
    return 100


def mode_words(cap):
    """mode -> short human label, merged across every pack with ``cap``.

    A pack without "mode_words" contributes nothing; a caller falling back
    to the bare mode name for an unlabelled mode is expected.
    """
    out = {}
    for pack in _discover():
        if pack["cap"] == cap:
            out.update(pack.get("mode_words") or {})
    return out


def _owner(cap, mode):
    """The pack providing cap+mode -- first wins, graph_for()'s precedence."""
    for pack in _discover():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return pack
    return None


def lane_kind(cap, mode):
    """The lane kind the owning pack needs: "comfy" (the default) or
    "process" (the core runs a local program; see runner.py)."""
    pack = _owner(cap, mode)
    return (pack.get("lane_kind") or "comfy") if pack else "comfy"


def bins(cap, mode):
    """The owning process pack's {role: executable_name}, or {} for a comfy
    pack. The executable NAMES live here, in the pack, never in the core."""
    pack = _owner(cap, mode)
    return (pack.get("bins") or {}) if pack else {}


def pack_dir(cap, mode):
    """The owning pack's own directory, for {pack} in a process run plan.
    Returns "" when the pack is synthetic (tests) and has no file."""
    pack = _owner(cap, mode)
    if not pack:
        return ""
    f = _PACK_FILES.get(pack["id"]) or ""
    return os.path.dirname(f)


def mode_room(cap, mode):
    """The room id the owning pack declares for this mode, or None."""
    pack = _owner(cap, mode)
    return (pack.get("mode_rooms") or {}).get(mode) if pack else None


def mode_note(cap, mode):
    """The owning pack's one-line "when to pick this" for a mode, or None."""
    pack = _owner(cap, mode)
    return (pack.get("mode_notes") or {}).get(mode) if pack else None


def prompt_guide(cap, mode):
    """The owning pack's per-mode instruction for how a prompt must be
    written for this mode (L5's prompt helper), or None -- same lookup
    shape as mode_note()."""
    pack = _owner(cap, mode)
    return (pack.get("prompt_guides") or {}).get(mode) if pack else None


def writer(cap, mode):
    """The owning pack's writing skill for a mode (see "writers" above), or
    None -- same lookup shape as prompt_guide()."""
    pack = _owner(cap, mode)
    return (pack.get("writers") or {}).get(mode) if pack else None


_THINK = ("<think>", "</think>")


WRITER_OPTIONS_MAX = 5
WRITER_OPTION_CHARS = 60


def _writer_line(line):
    """One reply line -> (KEY, value) or (None, None); **bold** keys allowed."""
    head, sep, value = line.strip().strip("*").partition(":")
    if not sep:
        return None, None
    return head.strip().strip("*").strip().upper(), value.strip().strip("*").strip()


def _writer_options(value, none):
    """An OPTIONS value -> 2-5 distinct short choices, or [] when it is not that."""
    out = []
    for o in (value or "").split("|"):
        o = o.strip().strip("\"'").strip()
        if o and o.upper() != none and len(o) <= WRITER_OPTION_CHARS and o not in out:
            out.append(o)
    return out[:WRITER_OPTIONS_MAX] if len(out) >= 2 else []


def parse_writer_reply(w, text):
    """Parse a writer's line-delimited reply ->
    {"question": str, "options": [..]} when it asks (options may be []),
    {"missing": str} when a writer with `pictures` says one is missing,
    else {"values": {field id: raw text}, "note": str}. A non-blank QUESTION
    line before the multiline key wins; an OPTIONS line anywhere after it
    gives the choices. The multiline key's value runs to the end (a NOTE line
    ends it); its none_token means "", its keep_token leaves it out. With
    several multiline keys, each part also ends at the next line that starts
    with one of the writer's keys not seen yet; a key that already appeared
    stays as text inside the part, so a repeated header never truncates
    lyrics. The parts may come in any order. Any other key whose value is blank or the
    none_token is left out. Raises ValueError when the reply holds neither a
    question nor the (first) multiline key."""
    text = text or ""
    if _THINK[1] in text:
        text = text.split(_THINK[1], 1)[1]
    keys, none = w["keys"], w["none_token"].upper()
    split = not isinstance(w["multiline"], str)
    multis = list(w["multiline"]) if split else [w["multiline"]]
    keep = (w.get("keep_token") or "").upper()
    values, note, started, part = {}, "", [], None   # part: [key, first-line value, lines]

    def close():
        body = "\n".join(([part[1]] if part[1] else []) + part[2]).strip()
        flat = body.upper().rstrip(".")
        if not (keep and flat == keep):
            values[keys[part[0]]] = "" if flat == none else body

    all_lines = text.splitlines()
    for i, line in enumerate(all_lines):
        head, value = _writer_line(line)
        if part is not None:
            if head == "NOTE" and "NOTE" not in keys:
                note = value
                close()
                part = None
                if split:
                    continue
                break
            if split and head in keys and head != part[0] and head not in started:
                close()
                part = None
            else:
                if not line.strip().startswith("```"):
                    part[2].append(line)
                continue
        if head is None:
            continue
        if head == "MISSING" and w.get("pictures") and not started and value and value.upper() != none:
            return {"missing": value}
        if head == "QUESTION" and not started and value and value.upper() != none:
            options = next((_writer_options(v, none) for h, v in map(_writer_line, all_lines[i + 1:])
                            if h == "OPTIONS"), [])
            return {"question": value, "options": options}
        if head == "NOTE" and "NOTE" not in keys:
            note = value if value.upper() != none else ""
        elif head in multis and head not in started:
            started.append(head)
            part = [head, value, []]
        elif head in keys and head not in multis and value and value.upper() != none:
            values[keys[head]] = value
    if part is not None:
        close()
    if multis[0] not in started:
        raise ValueError("no %s line" % multis[0])
    return {"values": values, "note": note}


def reviser(cap, mode):
    """The owning pack's fixing skill for a mode (see "revisers" above), or
    None -- same lookup shape as writer()."""
    pack = _owner(cap, mode)
    return (pack.get("revisers") or {}).get(mode) if pack else None


REVISER_FIXES = ("edit", "reroll")


def reviser_fixes(r):
    """A reviser's allowed FIX words -> {word: spec}: a `fixes` dict as it
    is; a `fixes` list (or none: edit/reroll) maps each word to None, the
    plain meaning (PROMPT refills the reviser's own `fills`)."""
    f = r.get("fixes")
    if isinstance(f, dict):
        return f
    return {word: None for word in (f or REVISER_FIXES)}


def edit_in(cap, mode):
    """The mode (same cap) that edits a finished result of this mode (see
    "edit_in" above), or None when there is none or it is not installed."""
    pack = _owner(cap, mode)
    target = (pack.get("edit_in") or {}).get(mode) if pack else None
    return target if target and _owner(cap, target) else None


def parse_reviser_reply(r, text):
    """Parse a reviser's line-delimited reply ->
    {"question": str} when it asks, else {"diagnosis", "fix", "prompt",
    "note", "tweak"} (blank ones ""), plus "settings" {field id: raw text}
    for a `fixes` spec with "settings". Every key is one line; any other
    line is ignored. Raises ValueError when FIX is not one of the
    reviser's `fixes` (edit/reroll by default), or PROMPT (or SETTINGS) is
    blank where that fix needs it."""
    text = text or ""
    if _THINK[1] in text:
        text = text.split(_THINK[1], 1)[1]
    keys = {k.upper() for k in r["keys"]}
    values = {}
    for line in text.splitlines():
        head, sep, value = line.strip().strip("*").partition(":")
        head = head.strip().strip("*").strip().upper()
        if not sep or head not in keys or head in values:
            continue
        value = value.strip().strip("*").strip()
        if head != "FIX" and value.upper() in ("NONE", "N/A", "-"):   # "none" is a fix word
            value = ""
        values[head] = value
    if values.get("QUESTION"):
        return {"question": values["QUESTION"]}
    fix = (values.get("FIX") or "").split()
    fix = fix[0].strip(".,;").lower() if fix else ""
    fixes = reviser_fixes(r)
    if fix not in fixes:
        raise ValueError("FIX is not one of %s" % " / ".join(fixes))
    spec = fixes[fix]
    if (spec is None or spec.get("fills")) and not values.get("PROMPT"):
        raise ValueError("no PROMPT")
    out = {"diagnosis": values.get("DIAGNOSIS", ""), "fix": fix, "prompt": values.get("PROMPT", ""),
           "note": values.get("NOTE", ""), "tweak": values.get("TWEAK", "")}
    if spec and spec.get("settings"):
        settings = {}
        for part in values.get("SETTINGS", "").split(";"):
            k, sep, v = part.partition("=")
            k, v = k.strip().lower(), v.strip()
            if sep and k and v:
                settings[k] = v
        if not settings:
            raise ValueError("no SETTINGS")
        out["settings"] = settings
    return out


ROOMS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rooms.json")


def rooms(path=None):
    """Task rooms (rooms.json, core config: task words only) merged with the
    modes the packs declare for them.

    Each room gets ``modes: [{cap, mode}]`` ordered by (cap_order, pack id,
    the pack's own ``graphs`` order). A mode whose room id rooms.json does not
    list -- or that declares no room at all (id ``<cap>-<mode>``) -- gets a
    room built from its own label, in its cap's group (the cap word
    upper-cased), ordered 1000+, so a stranger's pack still shows up. A room
    with no mode keeps ``modes: []`` (shown dimmed, never hidden). The cutting
    room (``kind: "cutting"``) is not a mode room and is passed through as-is.

    A missing or broken rooms.json must not take /api/engines down with it:
    it logs a warning and every mode falls back to a room built from its own
    label (the path above), so the page still reaches every engine.
    """
    try:
        with open(path or ROOMS_PATH) as f:
            base = json.load(f)
        if not isinstance(base, list) or not all(isinstance(r, dict) and r.get("id") for r in base):
            raise ValueError("expected a list of rooms, each with an id")
    except (OSError, ValueError) as e:
        logging.getLogger(__name__).warning(
            "rooms.json unusable (%s): %s -- building rooms from the packs alone", path or ROOMS_PATH, e)
        base = []
    out = [r if r.get("kind") == "cutting" else dict(r, modes=[]) for r in base]
    by_id = {r["id"]: r for r in out if r.get("kind") != "cutting"}

    entries, seen = [], set()
    for pack in _discover():
        for i, mode in enumerate(pack["graphs"]):
            key = (pack["cap"], mode)
            if key in seen or _owner(*key) is not pack:
                continue
            seen.add(key)
            entries.append(((cap_order(pack["cap"]), pack["id"], i), key))
    entries.sort(key=lambda e: e[0])

    extra = 0
    for _, (cap, mode) in entries:
        rid = mode_room(cap, mode) or "%s-%s" % (cap, mode)
        room = by_id.get(rid)
        if room is None:
            extra += 1
            room = {"id": rid, "name": mode_words(cap).get(mode, mode),
                    "group": cap_word(cap).upper(), "order": 1000 + extra,
                    "blurb": "", "empty": "", "modes": []}
            by_id[rid] = room
            out.append(room)
        room["modes"].append({"cap": cap, "mode": mode})
    out.sort(key=lambda r: r.get("order", 1000))
    return out


def fields(cap, mode):
    """Field descriptors for one mode -- [] if the owning pack declares none.

    First pack providing cap+mode wins, matching graph_for's own precedence.
    """
    for pack in _discover():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return (pack.get("fields") or {}).get(mode) or []
    return []


def presets(cap, mode):
    """Named parameter sets for one mode -- [] if the owning pack declares none.

    First pack providing cap+mode wins, matching fields()'s own precedence.
    """
    for pack in _discover():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return (pack.get("presets") or {}).get(mode) or []
    return []


def quality(cap, mode):
    """Quality tiers for one mode -- [] if the owning pack declares none.

    First pack providing cap+mode wins, matching fields()'s own precedence.
    """
    for pack in _discover():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return (pack.get("quality") or {}).get(mode) or []
    return []


def examples(cap, mode):
    """"Try this" examples for one mode -- [] if the owning pack declares none.

    First pack providing cap+mode wins, matching fields()/presets()'s own
    precedence.
    """
    for pack in _discover():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return (pack.get("examples") or {}).get(mode) or []
    return []


def mode_deps_reason(cap, mode):
    """The owning pack's own reason this mode can't run right now, or None.

    Unlike missing_words() (lane model files), this check is lane-independent
    -- a pack's `mode_deps` callable, when it declares one for this mode. A
    pack with no such declaration, or whose check passes, returns None here,
    same as every other "pack has nothing to say" case in this module.
    """
    pack = _owner(cap, mode)
    if not pack:
        return None
    fn = (pack.get("mode_deps") or {}).get(mode)
    return fn() if fn else None


def post_for(cap, mode):
    """The pack's post-render callable for one mode, or None if it has none.

    First pack providing cap+mode wins, matching fields()/presets()/
    quality()'s own precedence.
    """
    for pack in _discover():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return (pack.get("post") or {}).get(mode)
    return None


def post_words(cap, mode):
    """What the page calls a mode's `post` step (see "post_words" above), or
    None when the mode has no post step. Same precedence as post_for()."""
    for pack in _discover():
        if pack["cap"] == cap and mode in pack["graphs"]:
            if not (pack.get("post") or {}).get(mode):
                return None
            return (pack.get("post_words") or {}).get(mode) or "the finishing step"
    return None


def licence_for(cap, mode):
    """R6: the one licence entry that applies to a finished (cap, mode) job.

    Uses the same "first pack providing cap+mode wins" precedence as
    fields()/presets()/quality(). A pack whose licence is a list (audio)
    picks the entry naming this mode; one with no "modes" key covers the
    whole pack, same as licences() itself. None if the pack declares no
    licence at all.
    """
    for pack in _discover():
        if pack["cap"] != cap or mode not in pack["graphs"]:
            continue
        lic = pack.get("licence")
        if not lic:
            return None
        for entry in (lic if isinstance(lic, list) else [lic]):
            if entry.get("modes") is None or mode in entry["modes"]:
                return {"name": entry["name"], "shippable": entry["shippable"],
                        "attribution": entry["attribution"]}
        return None
    return None


def unet_loader(name):
    """ComfyUI node dict for loading a UNet by filename.

    Why the branch: UnetLoaderGGUF accepts unet_name only (passing
    weight_dtype is a validation error there), and UNETLoader cannot open
    a .gguf file at all.
    """
    if name.lower().endswith(".gguf"):
        return {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": name}}
    return {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": name, "weight_dtype": "default"},
    }


def quant_words(filename):
    """Short quantisation badge for a filename, or "".

    First substring match among the known tokens wins; short tokens are
    uppercased for display.
    """
    name = filename.lower()
    for token in ("nvfp4", "int8", "fp8", "w4a8", "int4", "gguf", "bf16", "fp16"):
        if token in name:
            return token.upper() if len(token) <= 5 else token
    return ""

def primary_role(cap, mode):
    """The role whose FILE is the headline for a mode, e.g. for a job record.

    The core wants to show "which model made this" without knowing any model.
    A pack declares {"primary": {mode: role}}; absent, the first role of that
    mode's ability is used.
    """
    for pack in _discover():
        if pack["cap"] != cap:
            continue
        prim = pack.get("primary") or {}
        if mode in prim:
            return prim[mode]
        roles = pack["provides"].get(mode)
        if roles:
            first = roles[0]
            return first[0] if isinstance(first, (list, tuple)) else first
    return ""
