"""Browser gate for FB-2, one box per room: in a writer mode (the mode's writer
writes for this mode, and the room's guide has a helper) the guide's input is
the only place to speak. Send posts /api/guide/skill, the draft fills the form
under "What the guide filled in" with no click, and Make is never pressed for
the user; a SAY reply just talks; a tweak after a hand edit rewrites from the
form as it is now (the owner's "Use these goes away" report). With no helper
the prompt box stays the main field and the guide is one line. A mode with no
writer (Clean-up) and the Cutting Room keep the chat as before. R8: "Fix it"
on the Make-time list has the guide rewrite (writer mode + helper), else keeps
the list up with a line and focuses a field.

Set BWF_FB2_SHOTS=<dir> to save the Music room after a stubbed draft at
1280x800 and 390 wide.

Run: python3 tests/test_fb2_ui.py
"""
import json
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
import _picture_server as ps  # noqa: E402
from _ui_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = os.environ.get("BWF_FB2_SHOTS")
REPO = ui.REPO
ROOMS = {r["id"]: r for r in json.load(open(os.path.join(REPO, "rooms.json"), encoding="utf-8"))}

helper = ThreadingHTTPServer(("127.0.0.1", 0), ps.FakeHelper)
threading.Thread(target=helper.serve_forever, daemon=True).start()
store = os.path.join(ui.SCRATCH, "lane")
os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
lane_port = ui.free_port()
ui.PROCS.append(subprocess.Popen([sys.executable, os.path.join(ui.HERE, "fixtures", "fake_comfy.py"), "--port",
                                  str(lane_port), "--store", store], cwd=REPO,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
ui.wait_up("http://127.0.0.1:%d/system_stats" % lane_port)


def start_server(name, with_helper):
    port = ui.free_port()
    data = os.path.join(ui.SCRATCH, "data_" + name)
    os.makedirs(data, exist_ok=True)
    cfg = {"title": "fb2 ui", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"],
                      "models": {"ace_unet": "ace.safetensors", "ace_clip1": "a.safetensors",
                                 "ace_clip2": "b.safetensors", "ace_vae": "v.safetensors"}}],
           "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0}}
    if with_helper:
        cfg["helper"] = {"url": "http://127.0.0.1:%d/v1" % helper.server_address[1], "model": "test-model",
                         "timeout_s": 10, "vision": False, "guide_default": "on"}
    path = os.path.join(ui.SCRATCH, "config_%s.json" % name)
    with open(path, "w") as f:
        json.dump(cfg, f)
    env = dict(os.environ, GENCENTER_CONFIG=path, GENCENTER_DATA=data)
    ui.PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                     stdout=open(os.path.join(ui.SCRATCH, "server_%s.log" % name), "w"),
                                     stderr=subprocess.STDOUT))
    url = "http://127.0.0.1:%d/" % port
    ui.wait_up(url + "api/health")
    return url


URL = start_server("brain", True)
URL_NONE = start_server("nobrain", False)

SONG = ("TAGS: country, slow, pedal steel, sad male vocals\nBPM: 76\nKEY: NONE\nDURATION: 150\nTIMESIG: NONE\n"
        "LANGUAGE: NONE\nLYRICS:\n[Verse]\nthe porch light is on\n\n[Chorus]\nbut nobody's home\n\n"
        "[Verse]\nthe dog still waits\n\n[Chorus]\nbut nobody's home")
SADDER = ("TAGS: country, very slow, pedal steel, broken male vocals\nBPM: 60\nKEY: NONE\nDURATION: NONE\n"
          "TIMESIG: NONE\nLANGUAGE: NONE\nLYRICS:\n[Verse]\nthe porch light is out\n\n[Chorus]\nand nobody's home\n\n"
          "[Verse]\nthe dog stopped waiting\n\n[Chorus]\nand nobody's home")
SAY = "SAY: Music3 writes longer songs; ACE-Step is quicker for short ones."
# Lyrics with no voice in the style: the song pack's Make-time check objects.
NO_VOICE = ("warm acoustic pop, gentle drums", "[Verse]\nSunlight on the water\n[Chorus]\nHold on")

POSTS = []   # (path, body) of every POST the page sent to the app


def record(req):
    if req.method == "POST" and "/api/" in req.url:
        path = "/api/" + req.url.split("/api/", 1)[1].split("?")[0]
        try:
            body = json.loads(req.post_data or "{}")
        except ValueError:
            body = {}
        POSTS.append((path, body))


def posted(path):
    return [b for p, b in POSTS if p == path]


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=False)


def enter(page, url, room_id, mode=None):
    page.goto("about:blank")
    page.goto(url + "#room=" + room_id, wait_until="networkidle", timeout=30000)
    page.wait_for_function("() => typeof GUIDE !== 'undefined' && GUIDE && GUIDE.guide", timeout=15000)
    if mode and page.evaluate("STATE.mode") != mode:
        page.evaluate("() => setEngineChipOpen(true)")
        page.check('#enginePicker input[data-mode="%s"]' % mode)
    page.wait_for_timeout(600)


def msgs(page, who):
    return page.eval_on_selector_all("#guideLog .guide-msg.from-%s .guide-text" % who, "els => els.map(e => e.textContent)")


def text_of(page, sel):
    """The element's text, or "" when it does not exist (so a RED run fails a check, not the run)."""
    el = page.query_selector(sel)
    return el.inner_text() if el else ""


def last_guide_msg(page):
    m = msgs(page, "guide")
    return m[-1] if m else ""


def values(page):
    return page.evaluate("() => Object.fromEntries(Array.from(document.querySelectorAll('#inspector [data-field-id]'))"
                         ".map(e => [e.dataset.fieldId, e.type === 'checkbox' ? e.checked : e.value]))")


def settle(page):
    page.wait_for_timeout(300)
    page.wait_for_function("() => document.querySelector('#guideThinking').hidden", timeout=15000)
    page.wait_for_timeout(400)


def send(page, text):
    """Type into the guide's one box and press Enter; wait for the reply to settle."""
    page.fill("#guideInput", text)
    page.press("#guideInput", "Enter")
    settle(page)


def inside(page, child, parent):
    return page.evaluate("([c, p]) => { const a = document.querySelector(c), b = document.querySelector(p);"
                         " return !!(a && b && b.contains(a)); }", [child, parent])


def fill_form(page, tags, lyrics):
    """Type into the prompt and lyrics by hand (opening What the guide filled in when it is there)."""
    page.evaluate("() => { const d = document.querySelector('#guideFilled'); if(d) d.open = true; }")
    page.fill("#promptBox", tags)
    page.fill("#f_lyrics", lyrics)


# R11: the whole ask fits in the compact box (a mirror of the textarea's text box, same font and width).
FITS = """() => { const t = document.querySelector('#guideInput'), cs = getComputedStyle(t);
            const d = document.createElement('div');
            ['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing', 'wordSpacing'].forEach(k => d.style[k] = cs[k]);
            d.style.whiteSpace = 'pre-wrap'; d.style.overflowWrap = 'break-word'; d.style.position = 'absolute';
            d.style.width = (t.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)) + 'px';
            d.textContent = t.placeholder; document.body.appendChild(d);
            const need = d.getBoundingClientRect().height; d.remove();
            const room = t.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
            return [need <= room + 0.5, need, room, t.placeholder]; }"""
OPEN = "() => !!(document.querySelector('#guideFilled') || {}).open"
# Top to bottom in a writer mode: Try this, the guide (its input), What the guide
# filled in (the prompt, the lyrics), then recipe / fields, then Make.
ORDER = ("() => { const q = s => document.querySelector(s);"
         " const els = ['#guidePanel', '#guideInput', '#guideFilled', '#promptBox', '#f_lyrics', '#recipeDetails',"
         "   '#primaryFields', '#makeBtn'].map(q);"
         " if(els.some(e => !e)) return 'missing: ' + els.map((e, i) => e ? '' : i).join(',');"
         " const tt = q('#tryThisWrap'); if(!tt.hidden) els.unshift(tt);"
         " for(let i = 1; i < els.length; i++){"
         "   if(!(els[i - 1].compareDocumentPosition(els[i]) & Node.DOCUMENT_POSITION_FOLLOWING)"
         "      && !els[i - 1].contains(els[i])) return 'dom ' + i;"
         "   const a = els[i - 1].getBoundingClientRect().top, b = els[i].getBoundingClientRect().top;"
         "   if(!(a <= b)) return 'top ' + i + ' ' + a + ' ' + b; }"
         " return true; }")

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", record)

        print("1: Music, song, a stub helper: Send writes, the fields fill with no click, Make is not pressed")
        enter(page, URL, "music", "song")
        check("song: #guideFilled is shown, holding the prompt and its content fields",
              page.is_visible("#guideFilled") and inside(page, "#promptBox", "#guideFilled")
              and inside(page, "#contentFields", "#guideFilled"))
        check("song: Help me write this is hidden (Send is the write)", not page.is_visible("#helperWriteBtn"))
        check("song: the one box asks for what the room should make",
              page.get_attribute("#guideInput", "placeholder") == "Tell the %s room what you want" % ROOMS["music"]["name"],
              page.get_attribute("#guideInput", "placeholder"))
        check("song: What the guide filled in is closed while it is empty",
              page.evaluate("() => { const d = document.querySelector('#guideFilled');"
                            " return !!d && typeof guideFilledAny === 'function' && d.open === guideFilledAny(); }"))
        del POSTS[:]
        del ps.HELPER_STATE["requests"][:]
        ps.HELPER_STATE["replies"][:] = [SONG]
        send(page, "a sad country song")
        skill = posted("/api/guide/skill")
        check("send: it posted /api/guide/skill with the typed words as the topic",
              len(skill) == 1 and skill[0].get("mode") == "song" and skill[0].get("topic") == "a sad country song"
              and (skill[0].get("context") or {}).get("mode") == "song", skill)
        check("send: nothing went to the chat", posted("/api/guide/chat") == [], posted("/api/guide/chat"))
        check("draft: the fields filled with no click",
              page.input_value("#promptBox") == "country, slow, pedal steel, sad male vocals"
              and page.input_value("#f_lyrics").startswith("[Verse]\nthe porch light is on")
              and page.input_value("#f_bpm") == "76", values(page))
        check("draft: no Use these to press", page.query_selector("#guideSkillUse") is None)
        check("draft: What the guide filled in is open", page.evaluate(OPEN))
        check("draft: the guide says what it filled", last_guide_msg(page).startswith("I filled in Style / genre")
              and "Lyrics" in last_guide_msg(page) and last_guide_msg(page).endswith("check them below, then press Make."),
              last_guide_msg(page))
        check("draft: the typed words are the user's turn in the log", "a sad country song" in msgs(page, "user"))
        check("draft (R10): no card after an auto-applied draft", not page.is_visible("#guideSkill"))
        check("draft (R10): Start over sits right under the guide's line", page.is_visible("#guideSkillRestart")
              and inside(page, "#guideSkillRestart", "#guideDraftBar") and page.query_selector("#guideSkillDismiss") is None)
        check("draft: focus stayed in the guide's box",
              page.evaluate("document.activeElement && document.activeElement.id") == "guideInput")
        check("draft: Make was NOT pressed", posted("/api/generate") == [], posted("/api/generate"))
        shot(page, "fb2-music-draft-1280x800")
        check("layout at 1280: guide, then what it filled in, then the rest, then Make", page.evaluate(ORDER) is True,
              page.evaluate(ORDER))

        print("owner: hand-edit one field, then a tweak -- it rewrites from the form as it is now")
        page.fill("#f_bpm", "72")
        ps.HELPER_STATE["replies"][:] = [SADDER]
        del POSTS[:]
        send(page, "make it slower and sadder")
        skill = posted("/api/guide/skill")
        ctx = ((skill[-1].get("context") or {}).get("fields") or {}) if skill else {}
        check("tweak: it posted /api/guide/skill, not the chat", len(skill) == 1 and posted("/api/guide/chat") == [],
              [p for p, _ in POSTS])
        check("tweak: the request carries the hand-edited value and the first draft",
              bool(skill) and skill[-1].get("topic") == "make it slower and sadder" and ctx.get("bpm") == 72
              and ctx.get("tags") == "country, slow, pedal steel, sad male vocals", skill[-1:])
        asked = ps.HELPER_STATE["requests"][-1]["messages"][-1]["content"] if ps.HELPER_STATE["requests"] else ""
        check("tweak: the helper saw the edited BPM on the room line", "BPM: 72" in asked, asked[:300])
        check("tweak: the second draft auto-applied",
              page.input_value("#promptBox") == "country, very slow, pedal steel, broken male vocals"
              and page.input_value("#f_bpm") == "60"
              and page.input_value("#f_lyrics").startswith("[Verse]\nthe porch light is out"), values(page))
        check("tweak: What the guide filled in is open", page.evaluate(OPEN))
        check("tweak: Make was NOT pressed", posted("/api/generate") == [])
        print("R10 (b): Start over, from the line under the draft, takes that write out of the conversation")
        filled_now = values(page)
        check("restart: the card is hidden, Start over is shown", not page.is_visible("#guideSkill")
              and page.is_visible("#guideSkillRestart"))
        if page.is_visible("#guideSkillRestart"):
            page.click("#guideSkillRestart")
            page.wait_for_timeout(300)
        check("restart: the tweak and its 'I filled in' line leave the log", "make it slower and sadder" not in msgs(page, "user")
              and msgs(page, "user")[-1:] == ["a sad country song"]
              and len([m for m in msgs(page, "guide") if m.startswith("I filled in")]) == 1, msgs(page, "guide")[-2:])
        check("restart: the fields stay as they are", values(page) == filled_now)

        print("2: a SAY reply just talks, nothing changes")
        before = values(page)
        ps.HELPER_STATE["replies"][:] = [SAY]
        del POSTS[:]
        send(page, "what's the difference between Music3 and ACE?")
        check("say: it went to the writer", len(posted("/api/guide/skill")) == 1)
        check("say: the transcript has the guide's words", last_guide_msg(page) == SAY[len("SAY: "):], last_guide_msg(page))
        check("say: no field changed", values(page) == before)
        check("say: no Make", posted("/api/generate") == [])

        print("R8 (1): Fix it, writer mode + helper: the guide rewrites against the list, no Make follows")
        fill_form(page, *NO_VOICE)
        del POSTS[:]
        page.click("#makeBtn")
        page.wait_for_selector("#makeConfirm:not([hidden])", timeout=15000)
        probs = page.eval_on_selector_all("#makeConfirmList li", "els => els.map(e => e.textContent)")
        check("fix: the Make-time list is up", len(probs) >= 1 and len(posted("/api/generate")) == 1, probs)
        ps.HELPER_STATE["replies"][:] = [SONG]
        page.click("#makeFixBtn")
        settle(page)
        skill = posted("/api/guide/skill")
        check("fix: Fix it posted /api/guide/skill with the problems in the topic", len(skill) == 1
              and skill[0].get("topic") == "Fix these before it renders: " + "; ".join(probs), skill)
        check("fix: the user's turn says Fix", ("Fix: " + "; ".join(probs)) in msgs(page, "user"), msgs(page, "user")[-1:])
        check("fix: the new values auto-applied", page.input_value("#promptBox") == "country, slow, pedal steel, sad male vocals"
              and page.input_value("#f_lyrics").startswith("[Verse]\nthe porch light is on"), values(page))
        check("fix: the list closed", not page.is_visible("#makeConfirm"))
        check("fix: the guide says it fixed them", last_guide_msg(page).startswith("I fixed ")
              and last_guide_msg(page).endswith("check them, then press Make."), last_guide_msg(page))
        check("fix: no /api/generate followed", len(posted("/api/generate")) == 1, len(posted("/api/generate")))

        print("6: the same order at 390 wide")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(400)
        check("layout at 390: the same order", page.evaluate(ORDER) is True, page.evaluate(ORDER))
        check("390: no horizontal scroll", page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth"))
        fits = page.evaluate(FITS)
        check("390 (R11): the compact box shows the whole placeholder", fits[0] is True, fits)
        page.locator("#guidePanel").scroll_into_view_if_needed()
        shot(page, "fb2-music-draft-390")
        page.set_viewport_size({"width": 1280, "height": 800})

        print("R9 (a): Characters: the Name the guide does not fill stays out, visible, under the guide")
        enter(page, URL, "characters")
        check("chars: a writer mode (What the guide filled in is shown)", page.is_visible("#guideFilled"))
        name = '#inspector [data-field-id="name"]'
        check("chars: the Name field is visible without opening anything", page.is_visible(name)
              and not page.evaluate(OPEN))
        check("chars: and it is outside #guideFilled", page.query_selector(name) is not None
              and not inside(page, name, "#guideFilled"))
        check("chars: between the guide and What the guide filled in", page.evaluate(
            "s => { const n = document.querySelector(s), g = document.querySelector('#guidePanel'),"
            " f = document.querySelector('#guideFilled'); return !!(n && g && f"
            " && (g.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING)"
            " && (n.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_FOLLOWING)); }", name))
        check("chars: with its own label", page.evaluate(
            "() => { const l = document.querySelector('#contentKeptWrap label[for=\"f_name\"]'); return !!(l && l.offsetParent) && l.textContent; }")
              == "Name", page.evaluate("() => ((document.querySelector('#contentKeptWrap') || {}).outerHTML || '').slice(0, 600)"))
        check("chars: the Sheet prompt (the writer's) is under What the guide filled in",
              inside(page, "#promptBox", "#guideFilled"))
        print("R12: the picture the writer reads sits above What the guide filled in, first, and on screen")
        ref = "#upload_reference"   # the charsheet writer's `pictures` field, read here from the page's own data
        check("chars: the writer's pictures field is the reference upload",
              page.evaluate("() => currentMode().writer.pictures") == "reference")
        order = page.evaluate("""() => { const q = s => document.querySelector(s);
            const up = q('#upload_reference'), nm = q('#inspector [data-field-id="name"]'), f = q('#guideFilled');
            if(!up || !nm || !f) return 'missing';
            const before = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
            return [before(q('#guidePanel'), up), before(up, nm), before(nm, f), !f.contains(up)]; }""")
        check("chars: guide, then the picture, then Name, then What the guide filled in", order == [True, True, True, True], order)
        on_screen = ("s => { const e = document.querySelector(s); if(!e || !e.offsetParent) return false;"
                     " const r = e.getBoundingClientRect(), c = document.querySelector('#inspector').getBoundingClientRect();"
                     " return r.top >= Math.max(0, c.top) && r.bottom <= Math.min(innerHeight, c.bottom); }")
        check("chars 1280: the picture upload is on screen without scrolling", page.evaluate(on_screen, ref),
              page.evaluate("s => { const e = document.querySelector(s); return e && [e.getBoundingClientRect().top, innerHeight]; }", ref))
        check("chars 1280: and so is Name", page.evaluate(on_screen, name),
              page.evaluate("s => { const e = document.querySelector(s); return e && [e.getBoundingClientRect().bottom, innerHeight]; }", name))
        fits = page.evaluate(FITS)
        check("chars 1280 (R11): the longer ask fits too", fits[0] is True, fits)
        shot(page, "fb2-characters-1280x800")

        print("4: Clean-up (no writer) and the Cutting Room keep the chat")
        enter(page, URL, "cleanup")
        check("cleanup: no What the guide filled in", not page.is_visible("#guideFilled"))
        check("cleanup: the input is the plain question box",
              page.get_attribute("#guideInput", "placeholder") == "Ask the guide. Enter sends, Shift+Enter for a new line.")
        ps.HELPER_STATE["replies"][:] = ["Pick a picture first."]
        del POSTS[:]
        send(page, "how do I cut out a background?")
        check("cleanup: Send posts /api/guide/chat", len(posted("/api/guide/chat")) == 1 and posted("/api/guide/skill") == [],
              [p for p, _ in POSTS])
        enter(page, URL, "cutting")
        check("cutting: the guide sits above the form, no What the guide filled in",
              page.evaluate("() => document.querySelector('#guidePanel').nextElementSibling === document.querySelector('#roomForm')")
              and not page.is_visible("#guideFilled"))
        ps.HELPER_STATE["replies"][:] = ["Start with a beat."]
        del POSTS[:]
        send(page, "where do I start?")
        check("cutting: Send posts /api/guide/chat", len(posted("/api/guide/chat")) == 1 and posted("/api/guide/skill") == [],
              [p for p, _ in POSTS])
        check("no page errors", errors == [], errors)
        page.close()

        print("3: no helper: the prompt box is the main field, the guide is one line")
        for w, h in ((1280, 800), (390, 844)):
            page = browser.new_page(viewport={"width": w, "height": h})
            page.on("request", record)
            enter(page, URL_NONE, "music", "song")
            check("%d off: the prompt box is visible, not inside a disclosure" % w, page.is_visible("#promptBox")
                  and not inside(page, "#promptBox", "#guideFilled") and not page.is_visible("#guideFilled"))
            check("%d off: the lyrics too" % w, page.is_visible("#f_lyrics"))
            check("%d off: the one line" % w, page.is_visible("#guideOffNote")
                  and text_of(page, "#guideOffNote") == "The guide is off, so type straight into the fields.")
            check("%d off: plus the add-a-helper line, nothing else of the guidance" % w, page.is_visible("#guideAddBrain")
                  and not page.is_visible("#guideNoBrainList") and not page.is_visible("#guideDefinition")
                  and not page.is_visible("#guideWriteNoBrain"))
            check("%d off: no Help me write this" % w, not page.is_visible("#helperWriteBtn"))
            check("%d off: the guide line sits above the fields" % w, page.evaluate(
                "() => !!(document.querySelector('#guidePanel').compareDocumentPosition(document.querySelector('#promptBox'))"
                " & Node.DOCUMENT_POSITION_FOLLOWING)"))
            if w == 1280:
                print("R8 (2): Fix it with no helper: the list stays, with a line, and a field has focus")
                fill_form(page, *NO_VOICE)
                del POSTS[:]
                page.click("#makeBtn")
                page.wait_for_selector("#makeConfirm:not([hidden])", timeout=15000)
                page.click("#makeFixBtn")
                page.wait_for_timeout(300)
                check("off fix: the list stays visible", page.is_visible("#makeConfirm")
                      and page.eval_on_selector_all("#makeConfirmList li", "els => els.length") >= 1)
                check("off fix: the line says what to do", page.is_visible("#makeFixHint")
                      and text_of(page, "#makeFixHint") == "Change what's listed, then press Make.")
                check("off fix: a field has focus", page.evaluate(
                    "() => { const a = document.activeElement; return !!(a && a.closest('#inspector')"
                    " && (a.dataset.fieldId || a.closest('[data-field-id]'))); }"),
                      page.evaluate("document.activeElement && document.activeElement.id"))
                check("off fix: nothing was sent to the guide, no second Make",
                      posted("/api/guide/skill") == [] and len(posted("/api/generate")) == 1, [p for p, _ in POSTS])
            page.close()
        browser.close()
finally:
    ui.finish()
